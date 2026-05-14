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
from datetime import date, datetime, timezone, timedelta
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

# day_active ATR threshold per market.
# HK large-caps (Tencent, Alibaba HK, etc.) have structurally lower daily ATR (~1-3%)
# vs CN/US which run 3-6%+. A single 4% floor excludes most quality HK names.
DAY_ACTIVE_ATR_MIN = {"CN": 0.04, "HK": 0.025, "US": 0.035}

# Guaranteed slots per market in the top-N candidate output.
# Prevents any single market from monopolising the pool when one region runs hard.
MARKET_MIN_SLOTS = {"CN": 25, "HK": 20, "US": 15}


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
    """Compute ret_5d, ret_20d, atr_pct, vol_surge, consolidation_ratio for one symbol.

    New metrics vs v1:
      vol_surge          — last bar volume / 20-day avg volume (>1 = accumulation)
      atr_5d_pct         — ATR over last 5 bars (shorter-term volatility)
      consolidation_ratio — atr_5d_pct / atr_pct; <0.85 means stock is coiling
                            (classic breakout setup: tightening range on rising price)
    """
    grp = grp.sort_values("date")
    close  = grp["close"].values
    high   = grp["high"].values
    low    = grp["low"].values
    volume = grp["volume"].values if "volume" in grp.columns else None
    n = len(close)

    cur = close[-1]
    ret_5d  = (cur - close[-min(6, n)]) / close[-min(6, n)] if n >= 2 else 0.0
    ret_20d = (cur - close[-min(21, n)]) / close[-min(21, n)] if n >= 2 else 0.0

    # 14-bar ATR (True Range)
    trs14 = []
    for i in range(max(1, n - 14), n):
        prev_c = close[i - 1]
        tr = max(high[i] - low[i], abs(high[i] - prev_c), abs(low[i] - prev_c))
        trs14.append(tr)
    atr = sum(trs14) / len(trs14) if trs14 else (high[-1] - low[-1])
    atr_pct = atr / cur if cur > 0 else 0.0

    # 5-bar ATR (most recent volatility — coiling detection)
    trs5 = []
    for i in range(max(1, n - 5), n):
        prev_c = close[i - 1]
        tr = max(high[i] - low[i], abs(high[i] - prev_c), abs(low[i] - prev_c))
        trs5.append(tr)
    atr_5 = sum(trs5) / len(trs5) if trs5 else atr
    atr_5d_pct = atr_5 / cur if cur > 0 else 0.0

    # Consolidation ratio: <0.85 means range is tightening vs 14-bar average (coiling)
    consolidation_ratio = (atr_5d_pct / atr_pct) if atr_pct > 0 else 1.0

    # Volume surge: last day vs 20-day average (excluding the last day itself)
    vol_surge = 1.0
    if volume is not None and len(volume) >= 5:
        vol_last = float(volume[-1])
        vol_avg  = float(volume[-min(21, n):-1].mean()) if n > 1 else vol_last
        vol_surge = (vol_last / vol_avg) if vol_avg > 0 else 1.0

    # 20d high for ambush detection
    high_20d = max(high[-min(21, n):])

    return {
        "current_price":      round(cur, 4),
        "ret_5d":             round(ret_5d, 6),
        "ret_20d":            round(ret_20d, 6),
        "atr_pct":            round(atr_pct, 6),
        "atr_5d_pct":         round(atr_5d_pct, 6),
        "consolidation_ratio": round(consolidation_ratio, 4),
        "vol_surge":          round(min(vol_surge, 10.0), 4),  # cap outliers
        "atr14":              round(atr, 4),
        "high_20d":           round(high_20d, 4),
        "days_in_cache":      n,
    }


def _priority_score(
    ret_5d: float,
    atr_pct: float,
    ret_20d: float,
    vol_surge: float,
    consolidation_ratio: float,
) -> float:
    """Composite short-term quality score (0-1 scale inputs → 0–100 range output).

    v2 vs v1: added vol_surge and consolidation to reward stocks that are
    building momentum (accumulation phase) rather than those already extended.

    Components:
      ret_5d × 25           — recent momentum (directional signal)
      atr_pct × 15          — volatility (tradeable range exists)
      ret_20d × 10          — medium-term trend alignment
      vol_surge_score × 30  — volume confirmation (the most reliable short-term signal)
      coiling_score × 20    — tight consolidation = higher-quality entry point

    vol_surge_score: 0 at 0.5× avg, 1.0 at 2.5×+ avg (linear clamp)
    coiling_score:   1.0 when atr_5d < 70% of atr_14d (tight coil), 0 when ratio ≥ 1.0
    """
    # Volume surge: 0 below 0.5×, ramps to 1.0 at 2.5×+
    vol_score = max(0.0, min(1.0, (vol_surge - 0.5) / 2.0))

    # Consolidation: reward tighter 5-bar ATR relative to 14-bar baseline
    coil_score = max(0.0, min(1.0, 1.0 - consolidation_ratio))

    raw = (ret_5d * 25 + atr_pct * 15 + ret_20d * 10
           + vol_score * 30 + coil_score * 20)
    return round(raw, 4)


def _assign_pool(row: dict) -> str:
    """Assign a stock to day_active / ambush / watch pool.

    day_active: actively moving stock suitable for 1-2 day trades.
    ambush:     deep pullback from 20d high — left-side swing entry opportunity.
    watch:      neither; monitor only.

    ATR threshold is market-specific:
      CN/US use 4%/3.5% (higher volatility markets)
      HK uses 2.5% (structurally lower ATR for Hang Seng large-caps)
    """
    ret_5d   = row["ret_5d"]
    atr_pct  = row["atr_pct"]
    vol_surge = row.get("vol_surge", 1.0)
    market   = row["market"]
    cur      = row["current_price"]
    high_20  = row["high_20d"]
    drawdown = (high_20 - cur) / high_20 if high_20 > 0 else 0.0

    atr_min = DAY_ACTIVE_ATR_MIN.get(market, 0.04)

    # Primary day_active: price momentum + volatility
    if ret_5d > 0.02 and atr_pct > atr_min:
        return "day_active"
    # Secondary: volume surge qualifies even moderate movers (accumulation breakout setup)
    if vol_surge >= 1.8 and ret_5d > 0.01 and atr_pct > atr_min * 0.7:
        return "day_active"
    if drawdown >= 0.20 and cur > MIN_PRICE.get(market, 1.0):
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

        score = _priority_score(
            m["ret_5d"], m["atr_pct"], m["ret_20d"],
            m["vol_surge"], m["consolidation_ratio"],
        )
        pool = _assign_pool({**m, "market": market})

        # Primary sector tag: prefer the first meaningful tag (split by ; then ,)
        raw_tags = info.get("sector_tags", "")
        sector = raw_tags.replace(",", ";").split(";")[0].strip() or market

        candidates.append({
            "symbol":              symbol,
            "yf_symbol":           info.get("yf_symbol", symbol),
            "company_name":        info.get("name", symbol),
            "market":              market,
            "sector":              sector,
            "sector_tags":         raw_tags,
            "chain_group":         sector,
            "role":                "candidate",
            "current_price":       cur,
            "ret_5d":              m["ret_5d"],
            "ret_20d":             m["ret_20d"],
            "atr_pct":             m["atr_pct"],
            "atr_5d_pct":          m["atr_5d_pct"],
            "consolidation_ratio": m["consolidation_ratio"],
            "vol_surge":           m["vol_surge"],
            "atr14":               m["atr14"],
            "drawdown_1y":         0.0,
            "turnover5":           3.0,
            "priority_score":      score,
            "rotation_score":      score,
            "pool":                pool,
            "is_loss":             info.get("is_loss", 0),
            "market_cap":          info.get("market_cap", 0),
            "days_in_cache":       m["days_in_cache"],
            "llm_thesis":          "",
        })

    # ── Market-diverse top-N selection ────────────────────────────────────────
    # Pure score-sort would let CN dominate (3357 stocks → most are CN).
    # Strategy: guarantee MARKET_MIN_SLOTS per market, then fill remaining by score.
    pool_order = {"day_active": 0, "ambush": 1, "watch": 2}
    sort_key = lambda x: (pool_order.get(x["pool"], 9), -x["priority_score"])  # noqa: E731

    by_market: dict[str, list[dict]] = {}
    for c in candidates:
        by_market.setdefault(c["market"], []).append(c)
    for mkt in by_market:
        by_market[mkt].sort(key=sort_key)

    result: list[dict] = []
    seen: set[str] = set()

    # Phase 1: reserved slots per market
    for mkt, min_slots in MARKET_MIN_SLOTS.items():
        pool_mkt = by_market.get(mkt, [])
        added = 0
        for c in pool_mkt:
            if added >= min_slots:
                break
            if c["symbol"] not in seen:
                result.append(c)
                seen.add(c["symbol"])
                added += 1
        if added:
            print(f"  {mkt}: reserved {added} slots (target {min_slots})")

    # Phase 2: fill remaining by global score
    all_sorted = sorted(candidates, key=sort_key)
    for c in all_sorted:
        if len(result) >= top_n:
            break
        if c["symbol"] not in seen:
            result.append(c)
            seen.add(c["symbol"])

    # Final sort for downstream consumers
    result.sort(key=sort_key)
    top = result[:top_n]

    print(f"Scored {len(candidates)} stocks → keeping top {len(top)}")
    pools: dict[str, int] = {}
    mkt_counts: dict[str, int] = {}
    for c in top:
        pools[c["pool"]] = pools.get(c["pool"], 0) + 1
        mkt_counts[c["market"]] = mkt_counts.get(c["market"], 0) + 1
    print(f"  Pools: {pools}")
    print(f"  Markets: {mkt_counts}")
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
        "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "total_screened": None,  # filled below
        "candidates": top,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved {len(top)} candidates → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
