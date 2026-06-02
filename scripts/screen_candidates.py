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
import os
import sqlite3
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _common import PROJECT_ROOT, load_env_file
from tradingagents.agents.rotation.three_locks import evaluate_three_locks
from tradingagents.runtime import read_fetch_manifest, today_cst

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

# Preserve a minimal amount of non-momentum inventory in the published top-N.
# Without this, a strong tape can fill the entire output with day_active names,
# starving downstream swing/watch flows even though valid ambush/watch candidates
# still exist deeper in the universe.
POOL_MIN_SLOTS = {"ambush": 5, "watch": 5}


def _fallback_allowed_latest_dates(market: str) -> set[str]:
    """Return the set of dates considered 'fresh enough' for screening.

    Always allow today AND yesterday for every market.

    Rationale:
    - Morning pipeline fires at 06:24 CST — before CN/HK open (09:30 CST) and
      before US even opens (21:30 CST).  At that time, the freshest available
      close data for CN/HK/US is from the previous trading day.  Restricting
      CN/HK to only "today" eliminates every CN stock from the pool before the
      market has had a chance to trade.
    - Fetch quality is enforced by fetch_all_daily.py (which always fetches the
      most recent session available).  The screener's job is to rank what was
      fetched, not second-guess it.
    - Allowing 1 prior calendar day handles weekends and holidays gracefully
      without needing market-calendar logic here.
    """
    today = datetime.now(timezone(timedelta(hours=8))).date()
    return {str(today), str(today - timedelta(days=1))}


def _allowed_latest_dates(market: str, manifest: dict | None) -> set[str]:
    """Return accepted latest dates for a market using the fetch manifest when present.

    `fetch_all_daily.py` already resolves per-market freshness, including the case
    where US evening should still use the prior US trading day while HK may already
    expose a same-day timestamp. Reuse that manifest instead of re-deriving a
    simpler calendar rule here.
    """
    coverage = (manifest or {}).get("coverage", {})
    accepted = coverage.get(market, {}).get("accepted_dates", [])
    dates = {str(value) for value in accepted if value}
    if dates:
        return dates
    return _fallback_allowed_latest_dates(market)


def _safe_text(value: object, fallback: str) -> str:
    """Return a clean string, falling back when CSV fields are NaN/blank."""
    if value is None or pd.isna(value):
        return fallback
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return fallback
    return text


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


def _select_candidates_with_diversity(candidates: list[dict], top_n: int) -> list[dict]:
    """Select top-N while preserving market and pool diversity."""
    pool_order = {"day_active": 0, "ambush": 1, "watch": 2}
    sort_key = lambda x: (pool_order.get(x["pool"], 9), -x["priority_score"])  # noqa: E731

    by_market: dict[str, list[dict]] = {}
    for candidate in candidates:
        by_market.setdefault(candidate["market"], []).append(candidate)
    for market in by_market:
        by_market[market].sort(key=sort_key)

    result: list[dict] = []
    seen: set[str] = set()

    # Phase 1: reserved slots per market
    for market, min_slots in MARKET_MIN_SLOTS.items():
        pool_mkt = by_market.get(market, [])
        added = 0
        for candidate in pool_mkt:
            if added >= min_slots or len(result) >= top_n:
                break
            if candidate["symbol"] in seen:
                continue
            result.append(candidate)
            seen.add(candidate["symbol"])
            added += 1
        if added:
            print(f"  {market}: reserved {added} slots (target {min_slots})")

    # Phase 1b: reserve a small slice for ambush/watch if those pools exist.
    for pool_name, min_slots in POOL_MIN_SLOTS.items():
        current = sum(1 for row in result if row["pool"] == pool_name)
        if current >= min_slots:
            continue
        pool_rows = sorted(
            [row for row in candidates if row["pool"] == pool_name],
            key=lambda row: row["priority_score"],
            reverse=True,
        )
        for row in pool_rows:
            if len(result) >= top_n or current >= min_slots:
                break
            if row["symbol"] in seen:
                continue
            result.append(row)
            seen.add(row["symbol"])
            current += 1

    # Phase 2: fill remaining by global score
    all_sorted = sorted(candidates, key=sort_key)
    for candidate in all_sorted:
        if len(result) >= top_n:
            break
        if candidate["symbol"] in seen:
            continue
        result.append(candidate)
        seen.add(candidate["symbol"])

    result.sort(key=sort_key)
    return result[:top_n]


def screen(top_n: int = TOP_N) -> list[dict]:
    if not DB_PATH.exists():
        # Hard-fail: sending a brief from an empty/stale database would be misleading.
        # The caller (local_pipeline.sh run_critical_step) will catch the non-zero exit.
        print("[ERROR] daily_cache.db not found — run fetch_all_daily.py first", flush=True)
        sys.exit(1)

    manifest = read_fetch_manifest()
    if not manifest:
        print("[ERROR] fetch_status.json missing — run fetch_all_daily.py first", flush=True)
        sys.exit(1)
    if manifest.get("trade_date") != today_cst():
        print(
            f"[ERROR] fetch_status.json stale for {manifest.get('trade_date')} — expected {today_cst()}",
            flush=True,
        )
        sys.exit(1)
    if manifest.get("status") != "ok":
        print(f"[ERROR] fetch_status.json status={manifest.get('status')} — abort scoring", flush=True)
        sys.exit(1)

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

    min_days = int(os.getenv("AI_ROTATOR_MIN_DAYS", str(MIN_DAYS)))
    candidates: list[dict] = []

    for (market, symbol), grp in prices.groupby(["market", "symbol"]):
        grp = grp.sort_values("date")
        latest_date = str(grp["date"].iloc[-1].date())
        if latest_date not in _allowed_latest_dates(market, manifest):
            continue
        if len(grp) < min_days:
            continue

        m = _compute_metrics(grp)
        three_locks = evaluate_three_locks(grp)
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
        raw_tags = _safe_text(info.get("sector_tags", ""), "")
        sector = raw_tags.replace(",", ";").split(";")[0].strip() or market
        company_name = _safe_text(info.get("name", symbol), symbol)
        yf_symbol = _safe_text(info.get("yf_symbol", symbol), symbol)

        candidates.append({
            "symbol":              symbol,
            "yf_symbol":           yf_symbol,
            "company_name":        company_name,
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
            "three_locks":         three_locks,
            "pool":                pool,
            "is_loss":             info.get("is_loss", 0),
            "market_cap":          info.get("market_cap", 0),
            "days_in_cache":       m["days_in_cache"],
            "llm_thesis":          "",
        })

    # ── Market-diverse + pool-diverse top-N selection ────────────────────────
    # Pure score-sort would let CN day_active names dominate in a strong tape.
    # Preserve enough ambush/watch inventory for downstream swing/watch consumers
    # while keeping day_active as the clear primary pool.
    top = _select_candidates_with_diversity(candidates, top_n)

    print(f"Scored {len(candidates)} stocks → keeping top {len(top)}")
    pools: dict[str, int] = {}
    mkt_counts: dict[str, int] = {}
    for c in top:
        pools[c["pool"]] = pools.get(c["pool"], 0) + 1
        mkt_counts[c["market"]] = mkt_counts.get(c["market"], 0) + 1
    print(f"  Pools: {pools}")
    print(f"  Markets: {mkt_counts}")
    return top, len(candidates)


def main() -> None:
    load_env_file()

    # Count scoreable universe before calling screen() so total_screened is accurate.
    import sqlite3 as _sqlite3
    _total = 0
    if DB_PATH.exists():
        with _sqlite3.connect(DB_PATH) as _conn:
            _total = _conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM daily_prices"
            ).fetchone()[0]

    top, scoreable_total = screen()
    if not top:
        print("[WARN] No candidates — cache may be empty", flush=True)
        sys.exit(1)

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "total_screened": scoreable_total,
        "candidates": top,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(
        f"Saved {len(top)} candidates (scoreable {scoreable_total} / cached {_total}) → {OUTPUT_JSON}",
        flush=True,
    )


if __name__ == "__main__":
    main()
