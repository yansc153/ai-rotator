from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4
import certifi
import requests  # handles SSL EOF gracefully on Python 3.14+

import yaml
from _common import PROJECT_ROOT, load_env_file
from storage.sqlite import insert_decision_ledger
from tradingagents.agents.rotation.execution_filter import (
    build_freshness_manifest,
    classify_candidate,
    session_meta as execution_session_meta,
    _earnings_payload_status,
)
from tradingagents.agents.rotation.shortline_enrichment import apply_shortline_enrichment
from tradingagents.contracts.decision_chain import CONTRACT_VERSION, DecisionLedgerRow, PushPayload
from tradingagents.runtime import read_fetch_manifest, today_cst

_CST = timezone(timedelta(hours=8))


def _today_cst() -> str:
    """Return today's date string in CST (UTC+8), e.g. '2026-05-15'.

    GitHub Actions runs in UTC; calling date.today() at 01:00 UTC (= 09:00 CST)
    returns the UTC date which is one day behind from the user's perspective.
    Always use this helper instead of str(date.today()) throughout this module.
    """
    return datetime.now(_CST).strftime("%Y-%m-%d")

def _session_meta(session: str) -> dict[str, Any]:
    meta = execution_session_meta(session)
    if not meta:
        raise RuntimeError(f"session_rules missing or invalid for session={session}")
    if "send_to_discord" not in meta:
        raise RuntimeError(f"session_rules missing send_to_discord for session={session}")
    return meta

def _load_intraday_overlay(market: str, symbol: str) -> dict[str, float]:
    """Return {ret_intraday, overextended} from latest 1h CSV for today.

    ret_intraday = (last_bar_close - first_bar_close_today) / first_bar_close_today
    Returns zeros if the file is missing or today has no bars (pre-open).
    """
    import pandas as pd
    from tradingagents.agents.rotation.common import RAW_DIR, normalize_symbol_for_file

    normalized = normalize_symbol_for_file(market, symbol)
    path = RAW_DIR / f"{market}_{normalized}_1h.csv"
    if not path.exists():
        return {"ret_intraday": 0.0, "overextended": False}
    try:
        df = pd.read_csv(path)
        if df.empty or "datetime" not in df.columns:
            return {"ret_intraday": 0.0, "overextended": False}
        today_str = _today_cst()
        today_bars = df[df["datetime"].str.startswith(today_str)]
        if today_bars.empty:
            return {"ret_intraday": 0.0, "overextended": False}
        open_close = float(today_bars.iloc[0]["close"])
        last_close = float(today_bars.iloc[-1]["close"])
        if open_close <= 0:
            return {"ret_intraday": 0.0, "overextended": False}
        ret_id = (last_close - open_close) / open_close
        return {"ret_intraday": ret_id, "overextended": False}  # caller sets overextended
    except Exception:
        return {"ret_intraday": 0.0, "overextended": False}


def _session_score(item: dict[str, Any], session: str) -> float:
    """Return session-adjusted score for re-ranking candidates.

    Three adjustments:
    1. Overbought penalty   — ret_5d > threshold removes the stock from contention
    2. Intraday momentum    — healthy same-day move boosts score
    3. Overextension penalty — already ran too far today = disqualified
    """
    cfg = _session_meta(session)
    base = float(item.get("rotation_score") or item.get("priority_score") or 0)

    # 1. Overbought filter (daily)
    ret_5d = float(item.get("ret_5d", 0.0))
    hard_overbought = float(cfg.get("hard_overbought_5d", 0.45))
    soft_overbought = float(cfg["overbought_5d"])
    if ret_5d > hard_overbought:
        # Extremely overbought — hard exclude
        overbought_penalty = base + 200.0
    elif ret_5d > soft_overbought:
        # Session-tuned overbought penalty.
        excess = ret_5d - soft_overbought
        overbought_penalty = excess * 180
    else:
        overbought_penalty = 0.0

    # 2 & 3. Intraday overlay
    intraday = _load_intraday_overlay(item.get("market", "US"), item.get("symbol", ""))
    ret_id = intraday["ret_intraday"]
    intraday_weight = cfg["intraday_weight"]

    overextended_threshold = cfg["overextended_intraday"]
    if ret_id > overextended_threshold:
        # Stock already ran too far today — strong penalty
        intraday_bonus = -50.0
    elif ret_id > 0.01:
        # Healthy momentum: +5 to +20 pts depending on move magnitude and session weight
        intraday_bonus = ret_id * 500 * intraday_weight
    elif ret_id < -0.02:
        # Down >2% today — penalise for short session, slight bonus for ambush
        pool = item.get("pool", "")
        intraday_bonus = 10.0 * intraday_weight if pool == "ambush" else -15.0 * intraday_weight
    else:
        # Flat or barely moved — neutral
        intraday_bonus = ret_id * 200 * intraday_weight

    return base - overbought_penalty + intraday_bonus


def _load_sector_aliases() -> dict[str, str]:
    path = PROJECT_ROOT / "config" / "sector_aliases.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("aliases", {})

SECTOR_ALIASES = _load_sector_aliases()

def _sector_cn(code: str) -> str:
    return SECTOR_ALIASES.get(code, code)

DISCORD_API_HOST = "https://discord.com/api/v10"
DISCORD_MESSAGE_LIMIT = 2000


def _load_rotation(date_str: str, market: str) -> dict[str, Any]:
    """Load a prebuilt daily rotation report for a given market."""
    path = PROJECT_ROOT / "reports" / "daily" / f"{date_str}-{market.lower()}-rotation.json"
    if not path.exists():
        raise FileNotFoundError(
            f"[rotation] Missing {market} rotation report for {date_str}: {path}. "
            "Run run_daily_rotation.py before send_discord_brief.py."
        )
    return json.loads(path.read_text())


def _cap_tier(market_cap: float) -> str:
    """Classify market cap into tiers to enforce diversity in pick selection."""
    if market_cap <= 0:
        return "unknown"
    if market_cap >= 500:     # ≥$500B / ≥500亿HKD equivalent
        return "mega"
    if market_cap >= 50:      # $50-500B
        return "large"
    if market_cap >= 5:       # $5-50B
        return "mid"
    return "small"            # <$5B


def _pick_with_diversity(candidates: list[dict], n: int) -> list[dict]:
    """Pick n items ensuring market diversity AND cap-tier diversity.

    Rules:
    1. Reserve 1 slot per market present (CN / HK / US)
    2. Cap mega-cap (≥$500B) stocks at max 2 slots total — prevents NVDA/AMD/TSM
       from filling all 5 short slots when US data improves
    3. Fill remaining slots by session-aware score descending

    Uses _session_score when present (set by build_brief_payload), otherwise
    falls back to rotation_score / priority_score.
    """
    def _score(item: dict) -> float:
        for key in ("shortline_priority_score", "execution_score", "_session_score"):
            if key in item:
                return float(item[key])
        return float(item.get("rotation_score") or item.get("priority_score") or 0)

    by_market: dict[str, list[dict]] = {}
    for item in candidates:
        by_market.setdefault(item["market"], []).append(item)

    result: list[dict] = []
    seen_symbols: set[str] = set()
    mega_cap_count = 0
    MEGA_CAP_MAX = 2  # at most 2 mega-cap stocks per block

    # Phase 1: guarantee 1 slot per market (pick highest-scored non-mega or mega if no choice)
    markets_present = list(by_market.keys())
    for market in markets_present:
        if len(result) >= n:
            break
        # Try non-mega first, then fall through to mega if no non-mega available
        pool = sorted(by_market[market], key=_score, reverse=True)
        for item in pool:
            if item["symbol"] in seen_symbols:
                continue
            tier = _cap_tier(float(item.get("market_cap", 0)))
            if tier == "mega" and mega_cap_count >= MEGA_CAP_MAX:
                continue
            result.append(item)
            seen_symbols.add(item["symbol"])
            if tier == "mega":
                mega_cap_count += 1
            break

    # Phase 2: fill remaining slots by score, respecting mega-cap cap
    remaining = sorted(candidates, key=_score, reverse=True)
    for item in remaining:
        if len(result) >= n:
            break
        if item["symbol"] in seen_symbols:
            continue
        tier = _cap_tier(float(item.get("market_cap", 0)))
        if tier == "mega" and mega_cap_count >= MEGA_CAP_MAX:
            continue
        result.append(item)
        seen_symbols.add(item["symbol"])
        if tier == "mega":
            mega_cap_count += 1

    # Phase 3: if still not enough, relax mega-cap constraint to fill remaining slots
    for item in remaining:
        if len(result) >= n:
            break
        if item["symbol"] not in seen_symbols:
            result.append(item)
            seen_symbols.add(item["symbol"])

    return result[:n]


def build_brief_payload(date_str: str, session: str = "morning") -> dict[str, Any]:
    us = _load_rotation(date_str, "US")
    ah = _load_rotation(date_str, "AH")
    if "sector_decision" not in us or "sector_decision" not in ah:
        raise RuntimeError("rotation report missing sector_decision contract")
    us_sector = us["sector_decision"]
    ah_sector = ah["sector_decision"]
    market_sector = {"US": us_sector, "CN": ah_sector, "HK": ah_sector}
    all_recs = [dict(row) for row in (us["recommendations"] + ah["recommendations"])]

    # Re-rank using session-aware scores (intraday overlay + overbought filter)
    meta = _session_meta(session)
    focus = meta.get("focus_markets")  # set of market codes, or None = all markets
    earnings_index, earnings_state = _earnings_payload_status(date_str)

    for rec in all_recs:
        rec["_session_score"] = _session_score(rec, session)
        sector_meta = market_sector[rec["market"]]
        leader_scores = {
            row["sector"]: float(row.get("score", 0.0))
            for row in sector_meta.get("leading_sectors", [])
        }
        rec["level1_rotation_regime"] = sector_meta.get("rotation_regime", "noisy")
        rec["level1_sector_score"] = leader_scores.get(rec["sector"], 0.0)
        rec["active_sector"] = bool(
            rec.get("active_sector", rec["sector"] in sector_meta.get("active_sector_ids", []))
        )
        rec["rank_in_sector"] = int(rec.get("rank_in_sector", 999))
        rec["sector_fit_score"] = float(
            rec.get("sector_fit_score", rec.get("rotation_score") or rec.get("priority_score") or 0.0)
        )
        rec.update(
            apply_shortline_enrichment(
                rec,
                session=session,
                earnings_index=earnings_index,
                earnings_state=earnings_state,
            )
        )

    classified: list[dict[str, Any]] = []
    for rec in all_recs:
        active_sector_ids = list(market_sector[rec["market"]].get("active_sector_ids", []))
        classified.append(
            classify_candidate(
                rec,
                session=session,
                trade_date=date_str,
                active_sector_ids=active_sector_ids,
                earnings_index=earnings_index,
                earnings_state=earnings_state,
            )
        )

    tradable_shorts = sorted(
        [r for r in classified if r["push_decision"] == "tradable_now" and r["horizon"] == "short"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )
    watch_shorts = sorted(
        [r for r in classified if r["push_decision"] == "watch_only" and r["horizon"] == "short"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )
    watch_swings = sorted(
        [r for r in classified if r["push_decision"] != "rejected" and r["horizon"] == "swing"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )
    rejected = sorted(
        [r for r in classified if r["push_decision"] == "rejected"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )

    short_block_size = int(meta.get("short_block_size", 5))
    coverage_watch_size = int(meta.get("coverage_watch_size", 3))
    if meta.get("short_block_source") == "watchlist":
        watchlist_pool = tradable_shorts + watch_shorts
        if meta.get("include_swing_in_watchlist", False):
            watchlist_pool += watch_swings
        short_pool = sorted(
            watchlist_pool,
            key=lambda x: float(x.get("shortline_priority_score", x.get("execution_score", 0.0))),
            reverse=True,
        )
    else:
        short_pool = tradable_shorts
    short_block = _pick_with_diversity(short_pool, short_block_size)
    short_syms = {item["symbol"] for item in short_block}
    coverage_watch = _pick_with_diversity(
        [item for item in watch_shorts if item["symbol"] not in short_syms],
        coverage_watch_size,
    )
    coverage_syms = {item["symbol"] for item in coverage_watch}
    swing_block = _pick_with_diversity(
        [item for item in watch_swings if item["symbol"] not in short_syms and item["symbol"] not in coverage_syms],
        3,
    )

    leaders: list[str] = []
    for market_code in ("CN", "HK", "US"):
        if focus is not None and market_code not in focus:
            continue
        for row in market_sector[market_code].get("leading_sectors", []):
            if row["sector"] not in leaders:
                leaders.append(row["sector"])
            if len(leaders) >= 3:
                break
        if len(leaders) >= 3:
            break
    signal = (ah["cross_market_signals"] or us["cross_market_signals"] or [{}])[0]
    freshness_manifest = build_freshness_manifest(all_recs, session, date_str)
    payload = {
        "run_id": str(uuid4()),
        "date": date_str,
        "session": session,
        "leaders": leaders,
        "cross_market_signal": signal,
        "short_block": short_block,
        "swing_block": swing_block,
        "coverage_watch": coverage_watch,
        "tradable_now": tradable_shorts,
        "watch_only": watch_shorts + watch_swings,
        "rejected": rejected,
        "freshness_manifest": freshness_manifest,
        "contract_version": CONTRACT_VERSION,
    }
    PushPayload.model_validate(payload)
    return payload


def _data_staleness_note() -> str:
    """Return a warning line if candidates.json is missing or was not generated today.

    New pipeline: fetch_all_daily → screen_candidates → candidates.json (with "date" field).
    Stale = generated on a previous date.  Missing = pipeline hasn't run yet.
    """
    try:
        candidates_path = PROJECT_ROOT / "data" / "candidates.json"
        if not candidates_path.exists():
            return "⚠️ 候选股票数据不存在 — 请先运行 fetch_all_daily.py + screen_candidates.py"
        data = json.loads(candidates_path.read_text())
        gen_date = data.get("date", "")
        if gen_date == _today_cst():
            return ""  # fresh
        return f"⚠️ 数据陈旧 — candidates.json 生成于 {gen_date}，建议运行 fetch_all_daily.py 更新"
    except Exception:
        pass
    return ""
def _load_earnings_plays(date_str: str, top_n: int = 3) -> list[dict]:
    """Load earnings plays from data/earnings_plays.json, return top_n for today.

    In calendar-only mode (data_limited=True), show up to top_n+2 extra plays
    since each card is shorter and the calendar itself is useful for planning.
    """
    path = PROJECT_ROOT / "data" / "earnings_plays.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if data.get("date") != date_str:
            return []  # stale — only use same-day data
        plays = data.get("earnings_plays", [])
        # In limited mode, show more plays (cards are compact)
        if plays and plays[0].get("data_limited"):
            return plays[:top_n + 2]
        return plays[:top_n]
    except Exception:
        return []


def _fmt_earnings_history(reactions: list[dict]) -> str:
    """Format last N post-earnings reactions as a compact string.

    e.g.  ▲+9.2%  ▼-3.1%  ▲+14.5%  ▼-0.8%  ▲+6.3%   (newest → oldest)
    """
    if not reactions:
        return "无历史数据"
    parts = []
    for r in reactions:
        pct = r.get("pct", 0)
        arrow = "▲" if pct >= 0 else "▼"
        parts.append(f"{arrow}{pct:+.1%}")
    return "  ".join(parts)


def _fmt_earnings_block(play: dict, idx: int, market_label: dict) -> list[str]:
    """Format one earnings play as Discord lines — v4 financial model.

    Displays:
      Line 1: ticker / name / market / side / earnings date / score
      Line 2: historical reactions + conviction
      Line 3: options-implied move vs historical edge  (options-payoff model)
      Line 4: EPS revision direction + beat rate       (estimate-analysis model)
      Line 5: entry plan (timing / buy zone / SL / T1 T2 / RR)
      Line 6: technical setup note
      Line 7: analyst rating  (optional)
    """
    mkt        = market_label.get(play["market"], play["market"])
    sec        = _sector_cn(play.get("sector", ""))
    side       = play.get("side", "LONG")
    side_emoji = "🟢多" if side == "LONG" else "🔴空"
    cur        = play.get("current_price", 0)
    win        = play.get("win_rate")
    conv       = play.get("conviction", "")
    timing     = play.get("timing", "")
    score      = play.get("total_score", 0)
    days       = play.get("days_to_earnings", 0)
    hist       = _fmt_earnings_history(play.get("historical_reactions", []))

    entry_low  = play.get("entry_low",  cur)
    entry_high = play.get("entry_high", cur)
    sl         = play.get("stop_loss",  cur)
    t1         = play.get("target_1",   cur)
    t2         = play.get("target_2",   cur)
    rr         = play.get("rr",         0)

    # Line 3: implied move edge (options-payoff model)
    implied_label = play.get("implied_label", "")
    if not implied_label:
        impl = play.get("implied_move")
        avg  = play.get("avg_move")
        if impl and avg:
            edge = avg - impl
            implied_label = f"期权隐含±{impl:.1%}  实际均±{avg:.1%}  超额{'+' if edge>=0 else ''}{edge:.1%}"
        elif impl:
            implied_label = f"期权隐含±{impl:.1%}"
        elif avg:
            implied_label = f"实际均±{avg:.1%}（无期权数据）"

    # Line 4: EPS revision (estimate-analysis model)
    rev_dir    = play.get("eps_revision_direction")
    rev_pct    = play.get("eps_revision_pct")
    beat_rate  = play.get("beat_rate")
    avg_surp   = play.get("avg_surprise_pct")
    rev_parts  = []
    if rev_dir and rev_pct is not None:
        arrow = "↑" if rev_dir == "上调" else ("↓" if rev_dir == "下调" else "→")
        rev_parts.append(f"EPS修正:{arrow}{rev_dir}{rev_pct:+.1%}(30日)")
    if beat_rate is not None:
        beat_str = f"过去{round(beat_rate*4)+1}季胜率{beat_rate:.0%}"
        if avg_surp is not None:
            beat_str += f" 均超预期{avg_surp:+.1%}"
        rev_parts.append(beat_str)
    eps_line = "  ".join(rev_parts) if rev_parts else "EPS修正数据不可用"

    # When data is limited (Yahoo Finance blocked), show a simpler calendar-only card.
    if play.get("data_limited"):
        lines = [
            f"#{idx} {play['symbol']} {play['company_name']} [{mkt}·{sec}]"
            f"  📅 财报:{play['earnings_date']}({days}天后) {play.get('release_time','').replace('time-','').replace('-',' ')}",
            f"   预测EPS:{play.get('eps_forecast','N/A')}  上年EPS:{play.get('eps_last_year','N/A')}"
            f"  季度:{play.get('fiscal_quarter','N/A')}",
            f"   ⚠️ 历史/期权/评分数据受限（行情接口不可用，仅展示财报日历）",
        ]
        return lines

    lines = [
        # Line 1: header
        f"#{idx} {play['symbol']} {play['company_name']} [{mkt}·{sec}]"
        f"  {side_emoji}  财报:{play['earnings_date']}({days}天后)  评分:{score:.0f}",
        # Line 2: historical reactions
        f"   历史{len(play.get('historical_reactions', []))}次财报次日: {hist}"
        + (f"  {'多' if side=='LONG' else '空'}胜率:{win:.0%} {conv}" if win is not None else f"  {conv}"),
        # Line 3: options-implied edge  ← options-payoff model
        f"   {implied_label}",
        # Line 4: EPS revision          ← estimate-analysis model
        f"   {eps_line}",
        # Line 5: entry plan
        f"   {timing}  |  买入 {entry_low:.2f}–{entry_high:.2f}  |  SL {sl:.2f}"
        f"  |  T1 {t1:.2f} T2 {t2:.2f}  |  RR 1:{rr:.1f}",
        # Line 6: technical setup
        f"   技术: {play.get('notes', {}).get('technical', '')}",
    ]
    analyst_note = play.get("notes", {}).get("analyst", "")
    if analyst_note:
        lines.append(f"   分析师: {analyst_note}")
    return lines


def _layer_summary(item: dict[str, Any]) -> str:
    parts: list[str] = []
    events = item.get("event_tags", [])
    warnings = item.get("warning_layer", [])
    if events:
        parts.append("事件:" + ",".join(events[:3]))
    if item.get("macro_overlay_score"):
        parts.append(f"宏观:{float(item['macro_overlay_score']):+.1f}")
    risk_score = float(item.get("risk_score", 0.0) or 0.0)
    parts.append(f"风险:{risk_score:+.1f}")
    if warnings:
        parts.append("警报:" + ",".join(warnings[:3]))
    return " | ".join(parts)


def build_brief_text(date_str: str, session: str = "morning", payload: dict[str, Any] | None = None) -> str:
    payload = payload or build_brief_payload(date_str, session)
    meta = _session_meta(session)
    cn_leaders = " > ".join(_sector_cn(s) for s in payload["leaders"]) if payload["leaders"] else "无"
    signal = payload["cross_market_signal"]
    signal_text = signal.get("narrative", "无跨市场信号")
    market_label = {"CN": "A股", "HK": "港股", "US": "美股"}
    staleness = _data_staleness_note()
    lines = [
        f"{meta['label']} · {date_str}",
        f"({meta['caption']})",
        f"今日领涨赛道：{cn_leaders}",
        f"跨市场信号：{signal_text}",
    ]
    if staleness:
        lines.append(staleness)
    emit_short = bool(meta.get("emit_short_block", True))
    emit_swing = bool(meta.get("emit_swing_block", True))
    emit_watch = bool(meta.get("emit_coverage_watch", True))
    emit_earnings = bool(meta.get("emit_earnings", True))

    offset = 1
    watchlist_mode = meta.get("brief_mode") == "watchlist"
    if emit_short:
        lines += [
            "",
            f"▌ {'今日优先盯盘 ticker' if watchlist_mode else '短线 1-2天'} (共{len(payload['short_block'])}只)",
        ]
        if watchlist_mode:
            lines.append("说明：这是开盘前盯盘清单，不是自动买入指令；盘中只执行你自己确认的突破/回踩。")
        for idx, item in enumerate(payload["short_block"], start=1):
            mkt = market_label.get(item["market"], item["market"])
            sec = _sector_cn(item["sector"])
            decision = "优先" if item.get("push_decision") == "tradable_now" else "观察"
            lines.append(f"#{idx} {item['symbol']} {item['company_name']} [{decision}] {mkt}·{sec}")
            if watchlist_mode:
                lines.append(
                    f"   现价 {item['current_price']:.2f} | 5日 {item.get('ret_5d', 0.0):+.1%} | "
                    f"ATR {item.get('atr_pct', 0.0):.1%} | pool={item.get('pool','watch')}"
                )
                lines.append(
                    f"   盯盘：{item.get('thesis') or sec + ' 强势 + ' + mkt + ' 动量延续'}"
                )
                lines.append(f"   加权：{_layer_summary(item)}")
            else:
                p = item["plan"]
                lines.append(
                    f"   现价 {item['current_price']:.2f} | 买入 {p['entry_low']:.2f}-{p['entry_high']:.2f} | "
                    f"T1 {p['target_1']:.2f} T2 {p['target_2']:.2f} | SL {p['stop_loss']:.2f} | RR 1:{p['rr']:.2f}"
                )
                lines.append(f"   触发：{item.get('thesis') or sec + ' 强势 + ' + mkt + ' 动量延续'}")
        offset += len(payload["short_block"])
    if emit_swing:
        lines.append("")
        lines.append(f"▌ 中长线 1-3月 (共{len(payload['swing_block'])}只)")
        for idx, item in enumerate(payload["swing_block"], start=offset):
            p = item["plan"]
            mkt = market_label.get(item["market"], item["market"])
            sec = _sector_cn(item["sector"])
            lines.append(f"#{idx} {item['symbol']} {item['company_name']} [LONG] {mkt}·{sec}")
            lines.append(
                f"   现价 {item['current_price']:.2f} | 三档买入 "
                f"{p['entry_tranches'][0]:.2f}/{p['entry_tranches'][1]:.2f}/{p['entry_tranches'][2]:.2f} | "
                f"SL {p['stop_loss']:.2f} | T1 {p['target_1']:.2f} T2 {p['target_2']:.2f}"
            )
            lines.append(f"   逻辑：{item.get('thesis') or sec + '中线布局机会'}")
        offset += len(payload["swing_block"])
    if emit_watch and payload.get("coverage_watch"):
        lines.append("")
        lines.append(f"▌ 市场覆盖观察名单 (共{len(payload['coverage_watch'])}只)")
        for idx, item in enumerate(
            payload["coverage_watch"],
            start=offset,
        ):
            mkt = market_label.get(item["market"], item["market"])
            sec = _sector_cn(item["sector"])
            lines.append(f"#{idx} {item['symbol']} {item['company_name']} [{mkt}·{sec}]")
            lines.append(
                f"   现价 {item['current_price']:.2f} | pool={item.get('pool','watch')} | "
                f"5日 {item.get('ret_5d', 0.0):+.1%} | ATR {item.get('atr_pct', 0.0):.1%}"
            )
            lines.append(
                f"   观察：{item.get('thesis') or sec + ' 值得跟踪'}，"
                "因市场覆盖需要单列展示，当前不归类为短线 1-2 天推荐。"
            )
        offset += len(payload["coverage_watch"])

    # ── 赌财报板块 ─────────────────────────────────────────────────────────────
    earnings_plays = _load_earnings_plays(date_str, top_n=3)
    if emit_earnings and earnings_plays:
        lines.append("")
        # Header: if any play is reporting today (0天后), say "本周发布", else "下周发布"
        _min_days = min((p.get("days_to_earnings", 99) for p in earnings_plays), default=99)
        _earn_window = "今日/本周" if _min_days <= 2 else "下周"
        _limited_note = " ⚠️限量数据" if earnings_plays[0].get("data_limited") else ""
        earnings_title = "▌ 🎯 赌财报"
        if earnings_plays[0].get("data_limited"):
            earnings_title = "▌ 📅 财报日历观察"
        lines.append(f"{earnings_title} — {_earn_window}发布 (共{len(earnings_plays)}只){_limited_note}")
        for idx, play in enumerate(earnings_plays, start=offset):
            lines.extend(_fmt_earnings_block(play, idx, market_label))
    return "\n".join(lines)


def _send_chunk(token: str, channel_id: str, text: str) -> None:
    """POST one message chunk to Discord using requests (handles SSL EOF on Python 3.14+)."""
    url = f"{DISCORD_API_HOST}/channels/{channel_id}/messages"
    payload = {"content": text, "allowed_mentions": {"parse": []}}
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "ai-rotator/1.0",
        },
        data=json.dumps(payload, ensure_ascii=False).encode(),
        verify=certifi.where(),
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Sent message id={result.get('id')}")


def maybe_send(text: str) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        print("[WARN] DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID not set — skipping real send")
        return
    # Split into 2000-char chunks if needed
    chunks = [text[i:i + DISCORD_MESSAGE_LIMIT] for i in range(0, len(text), DISCORD_MESSAGE_LIMIT)]
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            time.sleep(1.2)
        try:
            _send_chunk(token, channel_id, chunk)
        except requests.HTTPError as exc:
            print(f"[ERROR] Discord send failed: {exc.response.status_code} {exc.response.text[:200]}")
            raise


def _input_artifact_hash(date_str: str) -> str:
    digest = hashlib.sha256()
    candidates = PROJECT_ROOT / "data" / "candidates.json"
    us_report = PROJECT_ROOT / "reports" / "daily" / f"{date_str}-us-rotation.json"
    ah_report = PROJECT_ROOT / "reports" / "daily" / f"{date_str}-ah-rotation.json"
    earnings = PROJECT_ROOT / "data" / "earnings_plays.json"
    session_rules = PROJECT_ROOT / "config" / "session_rules.yaml"
    for path in (candidates, us_report, ah_report, earnings, session_rules):
        if path.exists():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _persist_decision_ledger(payload: dict[str, Any]) -> None:
    artifact_hash = _input_artifact_hash(payload["date"])
    rows: list[dict[str, Any]] = []
    all_items = payload.get("tradable_now", []) + payload.get("watch_only", []) + payload.get("rejected", [])
    for item in all_items:
        row = DecisionLedgerRow.model_validate(
            {
                "run_id": payload["run_id"],
                "trade_date": payload["date"],
                "session": payload["session"],
                "market": item["market"],
                "symbol": item["symbol"],
                "sector": item["sector"],
                "horizon": item.get("horizon", "short"),
                "level1_sector_score": item.get("level1_sector_score"),
                "level1_rotation_regime": item.get("level1_rotation_regime"),
                "level2_rank_in_sector": item.get("rank_in_sector"),
                "level2_sector_fit_score": item.get("sector_fit_score"),
                "level3_execution_score": item.get("execution_score"),
                "push_decision": item.get("push_decision", "rejected"),
                "push_reason": ",".join(item.get("reason_codes", [])) or item.get("push_decision", "rejected"),
                "reject_reason_codes": json.dumps(item.get("reason_codes", []), ensure_ascii=False),
                "contract_version": payload.get("contract_version", CONTRACT_VERSION),
                "input_artifact_hash": artifact_hash,
                "freshness_status": item.get("freshness_status"),
                "catalyst_status": item.get("catalyst_status"),
                "entry_triggered": None,
                "stop_hit": None,
                "target_1_hit": None,
                "target_2_hit": None,
                "mfe_pct": None,
                "mae_pct": None,
                "outcome_1d": None,
                "outcome_2d": None,
                "outcome_5d": None,
            }
        )
        rows.append(row.model_dump())
    insert_decision_ledger(rows)


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_today_cst())
    parser.add_argument("--session", default="morning",
                        choices=["morning", "ah_open", "midday", "evening"],
                        help="Which daily session to push (morning/ah_open/midday/evening)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = build_brief_payload(args.date, args.session)
    text = build_brief_text(args.date, args.session, payload=payload)
    if args.dry_run:
        print(text)
        return
    if not _session_meta(args.session).get("send_to_discord", True):
        print("[INFO] session configured as no-send warmup; skipping Discord push")
        print(text)
        return
    manifest = read_fetch_manifest()
    if manifest.get("trade_date") != today_cst() or manifest.get("status") != "ok":
        raise RuntimeError(f"[gate] fetch manifest invalid: {manifest}")
    if _session_meta(args.session).get("strict_send_requires_tradable", False) and not payload.get("short_block"):
        raise RuntimeError(
            f"[gate] {args.session} has zero tradable short-term signals after freshness/execution gating; refusing live push"
        )
    maybe_send(text)
    _persist_decision_ledger(payload)
    print(text)


if __name__ == "__main__":
    main()
