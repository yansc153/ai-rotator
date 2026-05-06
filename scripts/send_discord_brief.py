from __future__ import annotations

import argparse
import json
import os
import ssl
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import certifi

import yaml
from _common import PROJECT_ROOT, dump_json, load_env_file
from run_daily_rotation import build_rotation

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
        today_str = str(date.today())
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


def _pick_with_diversity(candidates: list[dict], n: int) -> list[dict]:
    """Pick n items ensuring at least 1 from each market that has candidates.

    Uses _session_score when present (set by build_brief_payload), otherwise
    falls back to rotation_score / priority_score.  This ensures the overbought
    filter and intraday overlay are honoured in both the guaranteed and fill slots.
    """
    def _score(item: dict) -> float:
        # Prefer _session_score (set per-session by build_brief_payload)
        if "_session_score" in item:
            return float(item["_session_score"])
        return float(item.get("rotation_score") or item.get("priority_score") or 0)

    by_market: dict[str, list[dict]] = {}
    for item in candidates:
        by_market.setdefault(item["market"], []).append(item)

    result: list[dict] = []
    seen_symbols: set[str] = set()

    # Reserve 1 slot per market (up to n), fill remainder by score
    markets_present = list(by_market.keys())
    guaranteed = min(len(markets_present), n)
    for market in markets_present[:guaranteed]:
        for item in by_market[market]:
            if item["symbol"] not in seen_symbols:
                result.append(item)
                seen_symbols.add(item["symbol"])
                break
        if len(result) >= n:
            break

    # Fill remaining slots by session-aware score descending
    remaining = sorted(candidates, key=_score, reverse=True)
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
    """Return a warning line if US market data is more than 2 trading days stale."""
    from datetime import date, timedelta
    try:
        import pandas as pd
        from tradingagents.agents.rotation.common import RAW_DIR
        cutoff = date.today() - timedelta(days=2)
        stale_count = 0
        checked = 0
        for csv_file in sorted(RAW_DIR.glob("US_*_daily.csv"))[:8]:
            try:
                last_date_str = pd.read_csv(csv_file, usecols=["date"]).dropna().tail(1)["date"].iloc[0]
                if pd.Timestamp(last_date_str).date() < cutoff:
                    stale_count += 1
                checked += 1
            except Exception:
                pass
        if checked == 0:
            return "⚠️ 无US市场数据 — 价格可能为模拟值，请运行 fetch_market_data.py"
        if stale_count >= checked // 2:
            return f"⚠️ 数据陈旧 — US数据最新至{last_date_str}，建议运行 fetch_market_data.py 更新"
    except Exception:
        pass
    return ""


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
    parser.add_argument("--date", default=str(date.today()))
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
