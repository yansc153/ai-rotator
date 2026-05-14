from __future__ import annotations

import argparse
import json
import os
import ssl
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import certifi

import yaml
from _common import PROJECT_ROOT, dump_json, load_env_file
from run_daily_rotation import build_rotation

_CST = timezone(timedelta(hours=8))


def _today_cst() -> str:
    """Return today's date string in CST (UTC+8), e.g. '2026-05-15'.

    GitHub Actions runs in UTC; calling date.today() at 01:00 UTC (= 09:00 CST)
    returns the UTC date which is one day behind from the user's perspective.
    Always use this helper instead of str(date.today()) throughout this module.
    """
    return datetime.now(_CST).strftime("%Y-%m-%d")

# ── Session configuration ─────────────────────────────────────────────────
# Each of the 3 daily pushes has a distinct label, focus, and scoring weights.
# "intraday_weight" controls how much today's 1h bar movement adjusts the base score.
SESSION_META = {
    "morning": {
        "label": "🌅 盘前早报",
        "caption": "美股昨收 + A/HK开盘布局",
        "intraday_weight": 0.0,        # Market not fully open; rely on daily scores
        "overbought_5d": 0.25,         # Penalise if 5d ret > 25%
        "overextended_intraday": 999,  # No intraday filter (no data yet)
        "focus_markets": None,         # All markets: overview of the whole day
    },
    "midday": {
        "label": "☀️ 盘中播报",
        "caption": "A/HK盘中动量 + 当日焦点",
        "intraday_weight": 0.6,        # Heavily weight what's actually moving today
        "overbought_5d": 0.20,         # Tighter overbought filter midday
        "overextended_intraday": 0.04, # Already up 4%+ today = skip
        "focus_markets": {"CN", "HK"}, # AH markets are live; show AH stocks only
    },
    "evening": {
        "label": "🌆 收盘晚报",
        "caption": "A/HK收盘复盘 + 美股夜盘预判",
        "intraday_weight": 0.4,
        "overbought_5d": 0.20,
        "overextended_intraday": 0.05,
        "focus_markets": {"US"},       # US market opening; show US overnight plays
    },
}

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
    cfg = SESSION_META.get(session, SESSION_META["morning"])
    base = float(item.get("rotation_score") or item.get("priority_score") or 0)

    # 1. Overbought filter (daily)
    ret_5d = float(item.get("ret_5d", 0.0))
    if ret_5d > 0.35:
        # Extremely overbought (>35% in 5 days) — hard exclude: score floor at -200
        overbought_penalty = base + 200.0
    elif ret_5d > 0.25:
        # Very overbought (25-35%): heavy penalty scales with excess
        excess = ret_5d - 0.25
        overbought_penalty = 80.0 + excess * 400  # 25%→80, 35%→120
    elif ret_5d > cfg["overbought_5d"]:
        # Moderately overbought: moderate penalty
        excess = ret_5d - cfg["overbought_5d"]
        overbought_penalty = excess * 300  # ~0-30 pts
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


def _ensure_rotation(date_str: str, market: str) -> dict[str, Any]:
    path = PROJECT_ROOT / "reports" / "daily" / f"{date_str}-{market.lower()}-rotation.json"
    if path.exists():
        return json.loads(path.read_text())
    payload = build_rotation(market, date_str)
    dump_json(path, payload)
    return payload


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
        if "_session_score" in item:
            return float(item["_session_score"])
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
    us = _ensure_rotation(date_str, "US")
    ah = _ensure_rotation(date_str, "AH")
    all_recs = us["recommendations"] + ah["recommendations"]

    # Re-rank using session-aware scores (intraday overlay + overbought filter)
    meta = SESSION_META.get(session, SESSION_META["morning"])
    focus = meta.get("focus_markets")  # set of market codes, or None = all markets

    for rec in all_recs:
        rec["_session_score"] = _session_score(rec, session)

    def _eligible(r: dict) -> bool:
        if focus is not None and r.get("market") not in focus:
            return False
        # Hard exclude: negative session_score means overbought / overextended
        if r.get("_session_score", 0) < 0:
            return False
        return True

    shorts = sorted(
        [r for r in all_recs if r["horizon"] == "short" and _eligible(r)],
        key=lambda x: x["_session_score"], reverse=True,
    )
    swings = sorted(
        [r for r in all_recs if r["horizon"] == "swing" and _eligible(r)],
        key=lambda x: x["_session_score"], reverse=True,
    )

    short_block = _pick_with_diversity(shorts, 5)  # up to 5 short, diverse markets

    # ── Guaranteed market slots in short block ────────────────────────────────
    # If a market (CN/HK/US) is absent from the short picks, pull its best
    # available candidate from any horizon so the brief covers all 3 markets.
    # This prevents scenarios where all 5 slots are taken by one region.
    markets_in_short = {r["market"] for r in short_block}
    all_sorted = sorted(all_recs, key=lambda x: x["_session_score"], reverse=True)
    seen_in_short = {r["symbol"] for r in short_block}
    for required_market in ("CN", "HK", "US"):
        if required_market in markets_in_short:
            continue
        if focus is not None and required_market not in focus:
            continue  # session doesn't cover this market — skip
        # Find the best eligible candidate from this market not already picked
        for candidate in all_sorted:
            if candidate.get("market") != required_market:
                continue
            if candidate["symbol"] in seen_in_short:
                continue
            if candidate.get("_session_score", 0) < 0:
                continue
            # Promote to short block: mark horizon override for display
            candidate = dict(candidate)
            candidate["horizon"] = "short"
            short_block.append(candidate)
            seen_in_short.add(candidate["symbol"])
            markets_in_short.add(required_market)
            print(f"[INFO] Promoted {candidate['symbol']} ({required_market}) to short block for market coverage")
            break

    # Swing: exclude symbols already in short_block
    short_syms = {i["symbol"] for i in short_block}
    unique_swings = [r for r in swings if r["symbol"] not in short_syms]
    swing_block = _pick_with_diversity(unique_swings, 3)  # up to 3 swing, no repeats

    leaders = [row["sector"] for row in (ah["leading_sectors_today"] + us["leading_sectors_today"])[:3]]
    signal = (ah["cross_market_signals"] or us["cross_market_signals"] or [{}])[0]
    return {
        "date": date_str,
        "session": session,
        "leaders": leaders,
        "cross_market_signal": signal,
        "short_block": short_block,
        "swing_block": swing_block,
    }


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
    """Load earnings plays from data/earnings_plays.json, return top_n for today."""
    path = PROJECT_ROOT / "data" / "earnings_plays.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if data.get("date") != date_str:
            return []  # stale — only use same-day data
        return data.get("earnings_plays", [])[:top_n]
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


def build_brief_text(date_str: str, session: str = "morning") -> str:
    payload = build_brief_payload(date_str, session)
    meta = SESSION_META.get(session, SESSION_META["morning"])
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
    lines += [
        "",
        f"▌ 短线 1-2天 (共{len(payload['short_block'])}只)",
    ]
    for idx, item in enumerate(payload["short_block"], start=1):
        p = item["plan"]
        mkt = market_label.get(item["market"], item["market"])
        sec = _sector_cn(item["sector"])
        lines.append(f"#{idx} {item['symbol']} {item['company_name']} [LONG] {mkt}·{sec}")
        lines.append(
            f"   现价 {item['current_price']:.2f} | 买入 {p['entry_low']:.2f}-{p['entry_high']:.2f} | "
            f"T1 {p['target_1']:.2f} T2 {p['target_2']:.2f} | SL {p['stop_loss']:.2f} | RR 1:{p['rr']:.2f}"
        )
        lines.append(f"   触发：{item.get('thesis') or sec + ' 强势 + ' + mkt + ' 动量延续'}")
    lines.append("")
    lines.append(f"▌ 中长线 1-3月 (共{len(payload['swing_block'])}只)")
    for idx, item in enumerate(payload["swing_block"], start=len(payload["short_block"]) + 1):
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

    # ── 赌财报板块 ─────────────────────────────────────────────────────────────
    earnings_plays = _load_earnings_plays(date_str, top_n=3)
    if earnings_plays:
        offset = len(payload["short_block"]) + len(payload["swing_block"]) + 1
        lines.append("")
        lines.append(f"▌ 🎯 赌财报 — 下周发布 (共{len(earnings_plays)}只)")
        for idx, play in enumerate(earnings_plays, start=offset):
            lines.extend(_fmt_earnings_block(play, idx, market_label))
    return "\n".join(lines)


def _send_chunk(token: str, channel_id: str, text: str) -> None:
    url = f"{DISCORD_API_HOST}/channels/{channel_id}/messages"
    req = Request(url, method="POST")
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "ai-rotator/1.0")
    body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode()
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    with urlopen(req, data=body, timeout=10, context=ssl_ctx) as resp:
        result = json.load(resp)
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
        except HTTPError as exc:
            print(f"[ERROR] Discord send failed: {exc.code} {exc.reason}")
            raise


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_today_cst())
    parser.add_argument("--session", default="morning",
                        choices=["morning", "midday", "evening"],
                        help="Which of the 3 daily sessions this push is for")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    text = build_brief_text(args.date, args.session)
    if args.dry_run:
        print(text)
        return
    maybe_send(text)
    print(text)


if __name__ == "__main__":
    main()
