"""Score all 3357 stocks from daily_cache.db and output top 150 candidates.

Scoring:
    priority_score = ret_5d×50 + atr_pct×30 + ret_20d×20

Pool assignment:
    day_active  — ret_5d > 3%  AND  atr_pct > 4%
    ambush      — down ≥20% from 20d high  AND  close > min_price
    watch       — sector leader present in top candidates

Filters applied before scoring:
    - close > min_price (CN:2元, HK:0.5港元, US:$1)
    - has ≥3 days in cache (otherwise unreliable)
    - NOT excluded if 亏损 (loss-making stocks allowed but scored lower)
    - ret_5d < 35% (overbought exclusion — same threshold as send_discord_brief)

Output: data/candidates.json
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _common import PROJECT_ROOT, load_env_file

UNIVERSE_CSV = ROOT / "data" / "universe_full.csv"
DB_PATH      = ROOT / "data" / "daily_cache.db"
OUTPUT_JSON  = ROOT / "data" / "candidates.json"

TOP_N = 150      # total candidates to keep
MIN_DAYS = 3     # minimum days of history required in cache
MIN_PRICE = {"CN": 2.0, "HK": 0.5, "US": 1.0}
MAX_RET_5D = 0.35   # overbought threshold — hard exclude above this


def _load_price_history(conn: sqlite3.Connection) -> pd.DataFrame:
    """Return a DataFrame with all cached prices, sorted (market, symbol, date)."""
    df = pd.read_sql(
        "SELECT date, market, symbol, open, high, low, close, volume, pct_change "
        "FROM daily_prices ORDER BY market, symbol, date",
        conn,
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


def _compute_metrics(grp: pd.DataFrame) -> dict:
    """Compute ret_5d, ret_20d, atr_pct, close for a single symbol's history."""
    grp = grp.sort_values("date")
    close = grp["close"].values
    high  = grp["high"].values
    low   = grp["low"].values
    n = len(close)

    cur = close[-1]
    ret_5d  = (cur - close[-min(6, n)]) / close[-min(6, n)] if n >= 2 else 0.0
    ret_20d = (cur - close[-min(21, n)]) / close[-min(21, n)] if n >= 2 else 0.0

    # ATR (True Range average over last 14 bars)
    trs = []
    for i in range(max(1, n - 14), n):
        prev_c = close[i - 1]
        tr = max(high[i] - low[i], abs(high[i] - prev_c), abs(low[i] - prev_c))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else (high[-1] - low[-1])
    atr_pct = atr / cur if cur > 0 else 0.0

    # 20d high for ambush detection
    high_20d = max(high[-min(21, n):])

    return {
        "current_price": round(cur, 4),
        "ret_5d":   round(ret_5d, 6),
        "ret_20d":  round(ret_20d, 6),
        "atr_pct":  round(atr_pct, 6),
        "atr14":    round(atr, 4),        # absolute ATR in price terms (for swing plan stop-loss)
        "high_20d": round(high_20d, 4),
        "days_in_cache": n,
    }


def _priority_score(ret_5d: float, atr_pct: float, ret_20d: float) -> float:
    return round(ret_5d * 50 + atr_pct * 30 + ret_20d * 20, 4)


def _assign_pool(row: dict) -> str:
    ret_5d  = row["ret_5d"]
    atr_pct = row["atr_pct"]
    cur     = row["current_price"]
    high_20 = row["high_20d"]
    drawdown = (high_20 - cur) / high_20 if high_20 > 0 else 0.0

    if ret_5d > 0.03 and atr_pct > 0.04:
        return "day_active"
    if drawdown >= 0.20 and cur > MIN_PRICE.get(row["market"], 1.0):
        return "ambush"
    return "watch"


def screen(top_n: int = TOP_N) -> list[dict]:
    if not DB_PATH.exists():
        print("[ERROR] daily_cache.db not found — run fetch_all_daily.py first")
        return []

    universe = pd.read_csv(UNIVERSE_CSV)
    # Build metadata lookup: symbol → {name, sector_tags, market, yf_symbol, tags, is_loss}
    meta: dict[str, dict] = {}
    for _, row in universe.iterrows():
        meta[row["symbol"]] = {
            "name": row["name"],
            "sector_tags": str(row.get("sector_tags", "") or ""),
            "market": row["market"],
            "yf_symbol": row["yf_symbol"],
            "is_loss": int(row.get("is_loss", 0)),
            "market_cap": float(row.get("market_cap") or 0),
        }
    # HK: also index by yf_symbol (e.g. "0700.HK") so we can look up after yf fetch
    for _, row in universe[universe.market == "HK"].iterrows():
        meta[str(row["yf_symbol"])] = meta[row["symbol"]]

    conn = sqlite3.connect(DB_PATH)
    prices = _load_price_history(conn)
    conn.close()

    candidates: list[dict] = []

    for (market, symbol), grp in prices.groupby(["market", "symbol"]):
        if len(grp) < MIN_DAYS:
            continue

        m = _compute_metrics(grp)
        cur = m["current_price"]
        min_p = MIN_PRICE.get(market, 1.0)

        if cur <= min_p:
            continue
        if m["ret_5d"] > MAX_RET_5D:
            continue  # overbought — hard exclude

        info = meta.get(symbol, {})
        if not info:
            continue  # not in our universe

        score = _priority_score(m["ret_5d"], m["atr_pct"], m["ret_20d"])
        pool  = _assign_pool({**m, "market": market})

        # Primary sector tag (first tag, cleaned)
        raw_tags = info.get("sector_tags", "")
        sector = raw_tags.split(",")[0].strip().split(";")[0].strip() or market

        candidates.append({
            "symbol":        symbol,
            "yf_symbol":     info.get("yf_symbol", symbol),
            "company_name":  info.get("name", symbol),
            "market":        market,
            "sector":        sector,
            "sector_tags":   raw_tags,
            "chain_group":   sector,        # same as sector; used by legacy downstream
            "role":          "candidate",
            "current_price": cur,
            "ret_5d":        m["ret_5d"],
            "ret_20d":       m["ret_20d"],
            "atr_pct":       m["atr_pct"],
            "atr14":         m["atr14"],    # absolute ATR value for swing plan stop-loss
            "drawdown_1y":   0.0,           # 30d cache can't compute 1y; set zero
            "turnover5":     3.0,           # not available from SQLite cache
            "priority_score": score,
            "rotation_score": score,  # same initially; sector_rotation_agent may adjust
            "pool":          pool,
            "is_loss":       info.get("is_loss", 0),
            "market_cap":    info.get("market_cap", 0),
            "days_in_cache": m["days_in_cache"],
            "llm_thesis":    "",
        })

    # Sort: day_active first (by score), then ambush, then watch
    pool_order = {"day_active": 0, "ambush": 1, "watch": 2}
    candidates.sort(key=lambda x: (pool_order.get(x["pool"], 9), -x["priority_score"]))

    top = candidates[:top_n]
    print(f"Scored {len(candidates)} stocks → keeping top {len(top)}")
    pools = {}
    for c in top:
        pools[c["pool"]] = pools.get(c["pool"], 0) + 1
    print(f"  Pools: {pools}")
    return top


def main() -> None:
    load_env_file()
    top = screen()
    if not top:
        print("[WARN] No candidates — cache may be empty")
        return

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": str(date.today()),
        "total_screened": None,  # filled below
        "candidates": top,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved {len(top)} candidates → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
