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
    """Pick n items ensuring at least 1 from each market that has candidates."""
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

    # Fill remaining slots by rotation_score descending
    remaining = sorted(candidates, key=lambda x: x.get("rotation_score") or x.get("priority_score", 0), reverse=True)
    for item in remaining:
        if len(result) >= n:
            break
        if item["symbol"] not in seen_symbols:
            result.append(item)
            seen_symbols.add(item["symbol"])

    return result[:n]


def build_brief_payload(date_str: str) -> dict[str, Any]:
    us = _ensure_rotation(date_str, "US")
    ah = _ensure_rotation(date_str, "AH")
    all_recs = us["recommendations"] + ah["recommendations"]

    shorts = sorted(
        [r for r in all_recs if r["horizon"] == "short"],
        key=lambda x: x.get("rotation_score") or x.get("priority_score", 0), reverse=True,
    )
    swings = sorted(
        [r for r in all_recs if r["horizon"] == "swing"],
        key=lambda x: x.get("rotation_score") or x.get("priority_score", 0), reverse=True,
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


def build_brief_text(date_str: str) -> str:
    payload = build_brief_payload(date_str)
    cn_leaders = " > ".join(_sector_cn(s) for s in payload["leaders"]) if payload["leaders"] else "无"
    signal = payload["cross_market_signal"]
    signal_text = signal.get("narrative", "无跨市场信号")
    market_label = {"CN": "A股", "HK": "港股", "US": "美股"}
    staleness = _data_staleness_note()
    lines = [
        f"🔄 AI轮动晨报 · {date_str}",
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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    text = build_brief_text(args.date)
    if args.dry_run:
        print(text)
        return
    maybe_send(text)
    print(text)


if __name__ == "__main__":
    main()
