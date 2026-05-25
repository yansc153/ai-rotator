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
from tradingagents.agents.rotation.bottleneck_scout import build_bottleneck_block
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


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "暂无"
    try:
        return f"{float(value):+.1%}"
    except (TypeError, ValueError):
        return "暂无"


def _fmt_price(value: Any) -> str:
    if value is None:
        return "暂无"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "暂无"


def _bucket_trade_style(item: dict[str, Any]) -> str:
    warnings = set(item.get("warning_layer", []))
    risk_score = float(item.get("risk_score", 0.0) or 0.0)
    if item.get("horizon") == "bottleneck":
        return "中线研究"
    if item.get("push_decision") == "rejected":
        return "回避"
    if item.get("pool") == "ambush" or item.get("horizon") == "swing":
        return "低吸观察"
    if {"extended_5d", "high_atr"} & warnings or risk_score <= -6.0:
        return "等确认"
    if item.get("push_decision") == "tradable_now":
        return "追强"
    return "隔夜观察"


def _item_reason(item: dict[str, Any]) -> str:
    thesis = str(item.get("thesis") or item.get("why_buy") or "").strip()
    if thesis:
        return thesis
    warnings = item.get("warning_layer", [])
    if warnings:
        return "注意 " + " / ".join(str(x) for x in warnings[:2])
    sector = _sector_cn(item.get("sector", ""))
    return f"{sector} 方向继续观察"


def _bucket_line(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "company_name": item.get("company_name"),
        "sector": item.get("sector"),
        "trade_style": _bucket_trade_style(item),
        "current_price": item.get("current_price"),
        "ret_5d": item.get("ret_5d"),
        "execution_score": float(item.get("execution_score", item.get("bottleneck_score", 0.0)) or 0.0),
        "reason": _item_reason(item),
        "pool": item.get("pool", ""),
    }


def _danger_reason(item: dict[str, Any]) -> str:
    warnings = list(item.get("warning_layer", []))
    if warnings:
        return " / ".join(str(x) for x in warnings[:3])
    reason_codes = list(item.get("reason_codes", []))
    if reason_codes:
        return " / ".join(str(x) for x in reason_codes[:3])
    if float(item.get("ret_5d", 0.0) or 0.0) >= 0.25:
        return "短线涨幅过大"
    return "等待结构确认"


def _build_mapping_chain(
    us_rotation: dict[str, Any],
    ah_rotation: dict[str, Any],
    *,
    session: str,
) -> list[dict[str, Any]]:
    del session
    rows = []
    for row in (ah_rotation.get("cross_market_signals", []) or []) + (us_rotation.get("cross_market_signals", []) or []):
        if not isinstance(row, dict):
            continue
        corr = float(row.get("correlation", row.get("score", 0.0)) or 0.0)
        best_lag = row.get("best_lag")
        narrative = str(row.get("narrative", "")).strip()
        if "->" in narrative:
            driver = narrative.split("->", 1)[0].strip()
        else:
            driver = "海外主线"
        rows.append(
            {
                "driver": driver,
                "mapped_asset": row.get("sector") or row.get("peer_name") or row.get("symbol") or "映射链",
                "lag": best_lag,
                "correlation": corr,
                "verified_event": bool(row.get("verified_event", False)),
                "note": narrative or "等待真实交易验证",
            }
        )
    rows.sort(key=lambda item: (not item["verified_event"], -abs(item["correlation"])))
    return rows[:3]


def _build_market_state(
    *,
    leaders: list[str],
    short_block: list[dict[str, Any]],
    coverage_watch: list[dict[str, Any]],
    tradable_now: list[dict[str, Any]],
    danger_pool: list[dict[str, Any]],
) -> dict[str, Any]:
    tradable_count = len([item for item in short_block if item.get("push_decision") == "tradable_now"])
    unique_sectors = {item.get("sector") for item in short_block + coverage_watch if item.get("sector")}
    avg_risk = sum(float(item.get("risk_score", 0.0) or 0.0) for item in short_block) / max(len(short_block), 1)
    avg_ret_5d = sum(float(item.get("ret_5d", 0.0) or 0.0) for item in short_block) / max(len(short_block), 1)

    if tradable_count == 0:
        regime = "风险偏好下降"
    elif len(unique_sectors) >= 3 and tradable_count >= 2:
        regime = "risk-on 扩散"
    elif avg_risk <= -5.0 or avg_ret_5d >= 0.22:
        regime = "高位拥挤"
    else:
        regime = "主线抱团"

    if tradable_count >= 2 and avg_risk > -4.0:
        mainline_health = "主线健康"
    elif tradable_count >= 1:
        mainline_health = "主线仍在但分化"
    else:
        mainline_health = "主线转弱"

    if len(unique_sectors) >= 3:
        breadth = "宽度扩散"
    elif len(short_block) <= 1:
        breadth = "指数强、内部弱"
    else:
        breadth = "主线分化"

    if regime == "risk-on 扩散":
        action_bias = "优先做强，但不追最末端"
    elif regime == "高位拥挤":
        action_bias = "只做确认，不做情绪末端追涨"
    elif tradable_count >= 1:
        action_bias = "等确认后参与"
    else:
        action_bias = "控仓观察"

    summary = (
        f"当前更像{regime}，主线状态为{mainline_health}，宽度表现为{breadth}。"
        f"今日有效短线机会 {tradable_count} 个，禁区池 {len(danger_pool)} 个，行动倾向：{action_bias}。"
    )

    return {
        "regime": regime,
        "mainline_health": mainline_health,
        "breadth": breadth,
        "action_bias": action_bias,
        "leader_count": len(leaders),
        "tradable_count": tradable_count,
        "avg_risk_score": round(avg_risk, 2),
        "avg_ret_5d": round(avg_ret_5d, 4),
        "summary": summary,
    }


def _build_open_script(
    *,
    session: str,
    short_block: list[dict[str, Any]],
    mapping_chain: list[dict[str, Any]],
    market_state: dict[str, Any],
) -> list[str]:
    leaders = [item.get("symbol") for item in short_block[:2] if item.get("symbol")]
    if session == "evening":
        script = [
            f"先看美债 10Y、QQQ/SMH 期货强弱，确认今晚是 {market_state['regime']} 还是继续高位拥挤。",
        ]
        if leaders:
            script.append(f"再看 {', '.join(leaders)} 开盘后是否继续强于板块，只有强者维持结构才考虑跟随。")
        if mapping_chain:
            script.append(f"最后看 {mapping_chain[0]['mapped_asset']} 这条映射链能否被次日 A/H 真实交易，不成立就只保留观察。")
        return script

    script = [
        f"先看指数与核心板块是否延续 {market_state['mainline_health']}，不要在 {market_state['regime']} 状态里做相反节奏。",
    ]
    if leaders:
        script.append(f"再看 {', '.join(leaders)} 是否率先弱转强或继续承接，决定今天先做追强还是等确认。")
    if mapping_chain:
        script.append(f"最后确认 {mapping_chain[0]['driver']} -> {mapping_chain[0]['mapped_asset']} 的映射是否成立，避免只追概念。")
    return script


def _build_decision_layers(
    *,
    us_rotation: dict[str, Any],
    ah_rotation: dict[str, Any],
    session: str,
    short_block: list[dict[str, Any]],
    coverage_watch: list[dict[str, Any]],
    swing_block: list[dict[str, Any]],
    bottleneck_block: list[dict[str, Any]],
    tradable_now: list[dict[str, Any]],
    watch_shorts: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    leaders: list[str],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    rejected_sorted = sorted(
        rejected + [item for item in short_block if {"extended_5d", "high_atr"} & set(item.get("warning_layer", []))],
        key=lambda item: float(item.get("execution_score", 0.0) or 0.0),
        reverse=True,
    )
    seen_danger: set[tuple[str, str]] = set()
    danger_pool: list[dict[str, Any]] = []
    for item in rejected_sorted:
        key = (str(item.get("market", "")), str(item.get("symbol", "")))
        if not key[0] or not key[1] or key in seen_danger:
            continue
        seen_danger.add(key)
        danger_pool.append(
            {
                "symbol": item.get("symbol"),
                "market": item.get("market"),
                "company_name": item.get("company_name"),
                "sector": item.get("sector"),
                "reason": _danger_reason(item),
                "trade_style": "回避",
                "current_price": item.get("current_price"),
                "ret_5d": item.get("ret_5d"),
            }
        )
        if len(danger_pool) >= 5:
            break

    overnight_candidates = watch_shorts + coverage_watch + swing_block
    overnight_seen: set[tuple[str, str]] = set()
    overnight_watch: list[dict[str, Any]] = []
    for item in overnight_candidates:
        key = (str(item.get("market", "")), str(item.get("symbol", "")))
        if not key[0] or not key[1] or key in overnight_seen:
            continue
        overnight_seen.add(key)
        overnight_watch.append(_bucket_line(item))
        if len(overnight_watch) >= 6:
            break

    daytrade_focus = [_bucket_line(item) for item in short_block[:5]]
    research_focus = [_bucket_line(item) for item in bottleneck_block[:5]]

    mapping_chain = _build_mapping_chain(us_rotation, ah_rotation, session=session)
    market_state = _build_market_state(
        leaders=leaders,
        short_block=short_block,
        coverage_watch=coverage_watch,
        tradable_now=tradable_now,
        danger_pool=danger_pool,
    )
    open_script = _build_open_script(
        session=session,
        short_block=short_block,
        mapping_chain=mapping_chain,
        market_state=market_state,
    )
    opportunity_buckets = {
        "daytrade_focus": daytrade_focus,
        "overnight_watch": overnight_watch,
        "research_focus": research_focus,
        "danger_pool": danger_pool,
    }
    return market_state, opportunity_buckets, danger_pool, mapping_chain, open_script


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
    bottleneck_block_size = int(meta.get("bottleneck_block_size", 0))
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
    bottleneck_block = build_bottleneck_block(
        (us.get("candidate_set", []) or [])
        + (ah.get("candidate_set", []) or [])
        + (us.get("recommendations", []) or [])
        + (ah.get("recommendations", []) or []),
        session=session,
        limit=bottleneck_block_size,
        focus_markets=focus,
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
    market_state, opportunity_buckets, danger_pool, mapping_chain, open_script = _build_decision_layers(
        us_rotation=us,
        ah_rotation=ah,
        session=session,
        short_block=short_block,
        coverage_watch=coverage_watch,
        swing_block=swing_block,
        bottleneck_block=bottleneck_block,
        tradable_now=tradable_shorts,
        watch_shorts=watch_shorts,
        rejected=rejected,
        leaders=leaders,
    )
    payload = {
        "run_id": str(uuid4()),
        "date": date_str,
        "session": session,
        "leaders": leaders,
        "cross_market_signal": signal,
        "short_block": short_block,
        "swing_block": swing_block,
        "bottleneck_block": bottleneck_block,
        "coverage_watch": coverage_watch,
        "tradable_now": tradable_shorts,
        "watch_only": watch_shorts + watch_swings + bottleneck_block,
        "rejected": rejected,
        "market_state": market_state,
        "opportunity_buckets": opportunity_buckets,
        "danger_pool": danger_pool,
        "mapping_chain": mapping_chain,
        "open_script": open_script,
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


def _fmt_bucket_item(item: dict[str, Any]) -> str:
    mkt = {"CN": "A股", "HK": "港股", "US": "美股"}.get(item.get("market"), item.get("market"))
    sec = _sector_cn(item.get("sector", ""))
    price = _fmt_price(item.get("current_price"))
    ret = _fmt_pct(item.get("ret_5d"))
    style = item.get("trade_style", "观察")
    reason = item.get("reason", "")
    score = item.get("execution_score")
    score_text = f" 评分:{float(score):.0f}" if score is not None else ""
    return f"{item.get('symbol')} [{mkt}·{sec}] {style}{score_text} | 现价 {price} | 5日 {ret} | {reason}"


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
    emit_bottleneck = bool(meta.get("emit_bottleneck_block", False))
    emit_watch = bool(meta.get("emit_coverage_watch", True))
    emit_earnings = bool(meta.get("emit_earnings", True))

    market_state = payload.get("market_state", {})
    buckets = payload.get("opportunity_buckets", {})
    if market_state:
        lines += [
            "",
            "▌ 30秒决策版",
            market_state.get("summary", "等待最新数据确认。"),
        ]
        if market_state.get("action_bias"):
            lines.append(f"动作倾向：{market_state['action_bias']}")
        if market_state.get("regime") or market_state.get("breadth") or market_state.get("mainline_health"):
            lines.append(
                f"市场状态：{market_state.get('regime','未知')} | 宽度：{market_state.get('breadth','未知')} | 主线：{market_state.get('mainline_health','未知')}"
            )

    daytrade_focus = buckets.get("daytrade_focus", [])
    if daytrade_focus:
        lines += [
            "",
            f"▌ 机会池｜日内优先 (共{len(daytrade_focus)}只)",
        ]
        for idx, item in enumerate(daytrade_focus, start=1):
            lines.append(f"#{idx} {_fmt_bucket_item(item)}")

    overnight_watch = buckets.get("overnight_watch", [])
    if overnight_watch:
        lines += [
            "",
            f"▌ 机会池｜隔夜观察 (共{len(overnight_watch)}只)",
        ]
        for idx, item in enumerate(overnight_watch, start=1):
            lines.append(f"#{idx} {_fmt_bucket_item(item)}")

    danger_pool = payload.get("danger_pool", [])
    if danger_pool:
        lines += [
            "",
            f"▌ 禁区池｜看起来强，但盈亏比差 (共{len(danger_pool)}只)",
        ]
        for idx, item in enumerate(danger_pool, start=1):
            lines.append(f"#{idx} {_fmt_bucket_item(item)}")

    mapping_chain = payload.get("mapping_chain", [])
    if mapping_chain:
        lines += [
            "",
            "▌ 关键映射链",
        ]
        for idx, row in enumerate(mapping_chain, start=1):
            corr = float(row.get("correlation", 0.0) or 0.0)
            lag = row.get("lag")
            lag_text = f"lag {lag}" if lag is not None else "lag 未知"
            verify = "已验证" if row.get("verified_event") else "待验证"
            lines.append(
                f"#{idx} {row.get('driver','海外主线')} → {row.get('mapped_asset','映射资产')} | corr {corr:+.3f} | {lag_text} | {verify}"
            )
            if row.get("note"):
                lines.append(f"   说明：{row['note']}")

    open_script = payload.get("open_script", [])
    if open_script:
        lines += [
            "",
            "▌ 开盘脚本",
        ]
        for idx, step in enumerate(open_script, start=1):
            lines.append(f"{idx}. {step}")

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
    if emit_bottleneck and payload.get("bottleneck_block"):
        lines.append("")
        lines.append(f"▌ 上游瓶颈侦察｜中长线 1-6月 (共{len(payload['bottleneck_block'])}只)")
        lines.append("说明：这是持有型研究清单，核心是产业链不可或缺性与证据升级，不是日内追涨信号。")
        for idx, item in enumerate(payload["bottleneck_block"], start=offset):
            mkt = market_label.get(item["market"], item["market"])
            sec = _sector_cn(item["sector"])
            lines.append(
                f"#{idx} {item['symbol']} {item['company_name']} [{mkt}·{sec}] "
                f"评分:{float(item.get('bottleneck_score', 0.0)):.0f}  周期:{item.get('time_horizon', '3-12月')}"
            )
            lines.append(
                f"   行情: 现价 {_fmt_price(item.get('current_price'))} | "
                f"5日 {_fmt_pct(item.get('ret_5d'))} | 20日 {_fmt_pct(item.get('ret_20d'))} | "
                f"来源:{item.get('source_pool', 'static_watchlist')}"
            )
            if item.get("why_buy"):
                lines.append(f"   买它: {item['why_buy']}")
            if item.get("hold_reason"):
                lines.append(f"   为什么持有: {item['hold_reason']}")
            if item.get("irreplaceable_role"):
                lines.append(f"   不可或缺角色: {item['irreplaceable_role']}")
            evidence = item.get("evidence", [])
            if evidence:
                lines.append("   证据: " + "；".join(str(x) for x in evidence[:2]))
            triggers = item.get("watch_triggers", [])
            if triggers:
                lines.append("   继续观察: " + "；".join(str(x) for x in triggers[:2]))
            invalid_if = item.get("invalid_if", [])
            if invalid_if:
                lines.append("   失效条件: " + "；".join(str(x) for x in invalid_if[:2]))
        offset += len(payload["bottleneck_block"])
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
    bottleneck = PROJECT_ROOT / "config" / "bottleneck_watchlist.yaml"
    session_rules = PROJECT_ROOT / "config" / "session_rules.yaml"
    for path in (candidates, us_report, ah_report, earnings, bottleneck, session_rules):
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
