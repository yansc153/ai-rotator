"""Batch-fetch daily OHLCV for all 3357 stocks and store in rolling SQLite cache.

Primary data source: Tencent Finance (qt.gtimg.cn) — accessible in CN network,
returns live quotes for CN/HK/US in a single batch HTTP call.

Strategy per market:
  CN  (2276) — Tencent Finance batch (200/call) → today's OHLCV;
                DB already holds rolling 30-day history from prior runs.
                Fallback: Yahoo chart direct if Tencent fails.
  HK  ( 702) — Tencent Finance batch (r_hkXXXXX) → today's OHLCV
  US  ( 379) — Tencent Finance batch (r_usSYMBOL) → latest close

NOTE: Direct calls from the user-selected stock-data skills are preferred over
      wrapper libraries. Tencent stays primary; Yahoo chart is the history fallback.

Cache: data/daily_cache.db  (SQLite)
Table: daily_prices(date, market, symbol, open, high, low, close, volume, pct_change)
Keep: last 30 calendar days (auto-pruned on each run)
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import argparse

import requests
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _common import PROJECT_ROOT, load_env_file
from tradingagents.data_sources import skill_market_data
from tradingagents.runtime import write_fetch_manifest

UNIVERSE_CSV = ROOT / "data" / "universe_full.csv"
DB_PATH = ROOT / "data" / "daily_cache.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

KEEP_DAYS = 30        # rolling window kept in SQLite
YF_PERIOD = "30d"     # Yahoo chart history window
YF_TIMEOUT = int(os.getenv("AI_ROTATOR_YF_TIMEOUT", "15"))
TENCENT_CHUNK = 200   # tickers per Tencent Finance API call
TENCENT_URL   = "http://qt.gtimg.cn/q={codes}"
TENCENT_RETRIES = 3
TENCENT_RETRY_SLEEP_S = 1.5
warnings.filterwarnings("ignore")

_CST = timezone(timedelta(hours=8))
MIN_MARKET_COVERAGE_RATIO = {"CN": 0.90, "HK": 0.95, "US": 0.90}


def _today_cst() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


def _previous_business_day(day: date) -> date:
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def _previous_business_days(day: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = day
    while len(days) < count:
        cursor = _previous_business_day(cursor)
        days.append(cursor)
    return days


def _accepted_trade_dates(
    market: str,
    now: datetime | None = None,
    *,
    session: str | None = None,
) -> set[str]:
    current = (now or datetime.now(_CST)).astimezone(_CST).date()
    prev = _previous_business_day(current)
    if market == "CN" and session == "midday":
        return {str(current)}
    if market == "CN" and session == "tail_close":
        return {str(current)}
    if market == "HK" and session == "midday":
        return {str(current)}
    if market == "HK" and session == "tail_close":
        return {str(current)}
    if market == "US":
        # US holidays are not captured by a simple weekday calendar. Accept a
        # short prior-business-day window so Monday holidays still use Friday's
        # close instead of degrading an otherwise healthy fetch run.
        return {str(day) for day in _previous_business_days(current, 3)}
    return {str(current), str(prev)}


def _effective_market_coverage(
    conn: sqlite3.Connection,
    market: str,
    accepted_dates: set[str],
) -> tuple[str | None, int]:
    if not accepted_dates:
        return None, 0
    ordered_dates = sorted(accepted_dates, reverse=True)
    placeholders = ",".join("?" for _ in ordered_dates)
    row = conn.execute(
        f"""
        SELECT date, COUNT(DISTINCT symbol) AS covered
        FROM daily_prices
        WHERE market = ? AND date IN ({placeholders})
        GROUP BY date
        ORDER BY date DESC
        LIMIT 1
        """,
        [market, *ordered_dates],
    ).fetchone()
    if not row:
        return None, 0
    return row[0], int(row[1] or 0)


# ── Tencent Finance batch fetcher (primary source, works in CN networks) ──────

def _tencent_parse_date(raw_ts: str) -> str | None:
    """Parse Tencent timestamp (YYYYMMDDHHMMSS or 'YYYY-MM-DD HH:MM:SS') → 'YYYY-MM-DD'."""
    raw_ts = raw_ts.strip()
    if not raw_ts:
        return None
    # Format A: '20260518132257'
    m = re.match(r'^(\d{4})(\d{2})(\d{2})\d{6}$', raw_ts)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Format B: '2026-05-15 16:00:02'
    m2 = re.match(r'^(\d{4}-\d{2}-\d{2})', raw_ts)
    if m2:
        return m2.group(1)
    return None


def _tencent_fetch_batch(
    codes: list[str],
    our_symbols: list[str],
    market: str,
    conn: sqlite3.Connection,
    vol_multiplier: float = 1.0,  # 100.0 for CN (lots → shares), 1.0 for HK/US
) -> int:
    """Fetch one batch of Tencent codes and upsert into DB.

    Field layout (0-indexed, ~ separated inside the quote string):
      [0]=type [1]=name [2]=code [3]=close [4]=prev_close [5]=open
      [6]=volume [30]=timestamp [31]=change [32]=pct_change [33]=high [34]=low
    """
    url = TENCENT_URL.format(codes=",".join(codes))
    text: str | None = None
    last_exc: Exception | None = None
    for attempt in range(1, TENCENT_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            text = resp.text
            break
        except Exception as exc:
            last_exc = exc
            if attempt < TENCENT_RETRIES:
                time.sleep(TENCENT_RETRY_SLEEP_S)
    if text is None:
        print(f"  Tencent {market} batch error: {last_exc}", flush=True)
        return 0

    code_to_sym = dict(zip(codes, our_symbols))
    rows: list[dict] = []

    for line in text.splitlines():
        # line format: v_CODE="fields";
        m = re.match(r'^v_(\S+?)="(.+?)";?$', line.strip())
        if not m:
            continue
        tencent_code = m.group(1)
        parts = m.group(2).split("~")
        if len(parts) < 35:
            continue
        try:
            close = float(parts[3])
            if close <= 0:
                continue
            prev_close = float(parts[4]) if parts[4] else close
            open_ = float(parts[5]) if parts[5] else close
            vol_raw = float(parts[6]) if parts[6] else 0.0
            high = float(parts[33]) if parts[33] else close
            low = float(parts[34]) if parts[34] else close
            pct = float(parts[32]) if parts[32] else 0.0
            ts = _tencent_parse_date(parts[30]) or _today_cst()
        except (ValueError, IndexError):
            continue

        # Try to resolve our symbol; fall back to code-level guessing
        our_sym = code_to_sym.get(tencent_code)
        if our_sym is None:
            continue

        rows.append({
            "date":       ts,
            "market":     market,
            "symbol":     our_sym,
            "open":       open_,
            "high":       high,
            "low":        low,
            "close":      close,
            "volume":     vol_raw * vol_multiplier,
            "pct_change": pct,
        })

    return _upsert(conn, rows)


def _tencent_fetch_all(
    tencent_codes: list[str],
    our_symbols: list[str],
    market: str,
    conn: sqlite3.Connection,
    vol_multiplier: float = 1.0,
) -> int:
    """Fetch all tickers in TENCENT_CHUNK-sized batches."""
    total = 0
    n = len(tencent_codes)
    n_chunks = max(1, (n - 1) // TENCENT_CHUNK + 1)
    for i in range(0, n, TENCENT_CHUNK):
        batch_codes = tencent_codes[i: i + TENCENT_CHUNK]
        batch_syms  = our_symbols[i: i + TENCENT_CHUNK]
        saved = _tencent_fetch_batch(batch_codes, batch_syms, market, conn, vol_multiplier)
        total += saved
        chunk_num = i // TENCENT_CHUNK + 1
        print(f"  Tencent {market} chunk {chunk_num}/{n_chunks}: {saved} rows", flush=True)
    return total


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_prices (
            date        TEXT NOT NULL,
            market      TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      REAL,
            pct_change  REAL,
            PRIMARY KEY (date, market, symbol)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dp ON daily_prices(market, symbol, date)")
    conn.commit()
    return conn


def _prune_old(conn: sqlite3.Connection) -> None:
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    conn.execute("DELETE FROM daily_prices WHERE date < ?", (cutoff,))
    conn.commit()


def _upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO daily_prices
           (date, market, symbol, open, high, low, close, volume, pct_change)
           VALUES (:date, :market, :symbol, :open, :high, :low, :close, :volume, :pct_change)""",
        rows,
    )
    conn.commit()
    return len(rows)


def _latest_trade_date(conn: sqlite3.Connection, market: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) FROM daily_prices WHERE market = ?",
        (market,),
    ).fetchone()
    return row[0] if row and row[0] else None


# ── CN helpers ────────────────────────────────────────────────────────────────

# ── CN fetch ─────────────────────────────────────────────────────────────────

def fetch_cn(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """Fetch CN stocks via Tencent Finance (primary) + Yahoo chart fallback.

    Tencent Finance (qt.gtimg.cn) is accessible from CN networks and returns live
    quotes for all A-share stocks. Yahoo chart is attempted only as a last-resort
    history fallback.

    History accumulates day-by-day in the DB; no 30-day bulk fetch is attempted.
    """
    cn = universe[universe.market == "CN"]
    cn_syms = cn["symbol"].tolist()

    # Build Tencent codes: '600941.SH' → 'sh600941', '300750.SZ' → 'sz300750'
    def _to_tencent(sym: str) -> str:
        if sym.endswith(".SH"):
            return "sh" + sym[:6]
        if sym.endswith(".SZ"):
            return "sz" + sym[:6]
        return sym  # fallback

    tencent_codes = [_to_tencent(s) for s in cn_syms]

    print(f"  CN: {len(tencent_codes)} tickers via Tencent Finance ...", flush=True)
    t0 = time.time()
    saved = _tencent_fetch_all(
        tencent_codes, cn_syms, "CN", conn,
        vol_multiplier=100.0,  # Tencent reports CN volume in lots (手), ×100 → shares
    )
    elapsed = time.time() - t0
    print(f"  CN Tencent: {saved} rows in {elapsed:.1f}s", flush=True)

    if saved == 0:
        yf_symbols = [str(v) for v in cn["yf_symbol"].tolist() if str(v)]
        yf_to_sym = {
            str(row["yf_symbol"]): row["symbol"]
            for _, row in cn.iterrows()
            if str(row.get("yf_symbol", "") or "")
        }
        print("  CN: Tencent returned 0 rows — trying Yahoo chart direct fallback ...", flush=True)
        saved = _yahoo_batch(yf_symbols, "CN", conn, ticker_to_symbol=yf_to_sym)

    return saved


def _yahoo_batch(
    tickers: list[str],
    market: str,
    conn: sqlite3.Connection,
    ticker_to_symbol: dict[str, str] | None = None,
) -> int:
    """Direct Yahoo chart fallback from global-stock-data, no yfinance wrapper."""
    total = 0
    for i, ticker in enumerate(tickers, 1):
        try:
            rows = skill_market_data.yahoo_chart(ticker, interval="1d", range_=YF_PERIOD, timeout=YF_TIMEOUT)
        except Exception as exc:
            print(f"  {market} yahoo {i}/{len(tickers)} {ticker}: error — {exc}", flush=True)
            continue
        out_rows = []
        resolved = ticker_to_symbol.get(ticker, ticker) if ticker_to_symbol else ticker
        prev_close = None
        for row in rows:
            close = float(row["close"])
            pct = (close - prev_close) / prev_close * 100 if prev_close else 0.0
            prev_close = close
            out_rows.append(
                {
                    "date": str(row["datetime"])[:10],
                    "market": market,
                    "symbol": resolved,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": close,
                    "volume": float(row["volume"]),
                    "pct_change": round(pct, 4),
                }
            )
        saved = _upsert(conn, out_rows)
        total += saved
        print(f"  {market} yahoo {i}/{len(tickers)} {ticker}: {saved} rows stored", flush=True)
    return total


def fetch_hk(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """Fetch HK stocks via Tencent Finance (r_hkXXXXX format, 5-digit code).

    Uses the legacy akshare_symbol column, which stores the 5-digit HK code.
    """
    hk = universe[universe.market == "HK"].copy()
    # Use existing 5-digit HK code column when available, else derive from symbol.
    hk_syms = hk["symbol"].tolist()
    ak_codes = []
    for _, row in hk.iterrows():
        ak_code = str(row.get("akshare_symbol", "") or "")
        if not ak_code:
            # Derive from symbol: "00700.HK" → "00700"
            ak_code = row["symbol"].split(".")[0]
        ak_codes.append(ak_code)

    tencent_codes = ["r_hk" + c for c in ak_codes]

    print(f"  HK: {len(tencent_codes)} tickers via Tencent Finance ...", flush=True)
    t0 = time.time()
    saved = _tencent_fetch_all(tencent_codes, hk_syms, "HK", conn, vol_multiplier=1.0)
    print(f"  HK Tencent: {saved} rows in {time.time()-t0:.1f}s", flush=True)
    if saved == 0:
        yf_symbols = []
        ticker_to_symbol: dict[str, str] = {}
        for _, row in hk.iterrows():
            yf_symbol = str(row.get("yf_symbol", "") or "")
            if not yf_symbol:
                continue
            yf_symbols.append(yf_symbol)
            ticker_to_symbol[yf_symbol] = row["symbol"]
        print("  HK: Tencent returned 0 rows — trying Yahoo chart direct fallback ...", flush=True)
        saved = _yahoo_batch(
            yf_symbols,
            "HK",
            conn,
            ticker_to_symbol=ticker_to_symbol,
        )
    return saved


def fetch_us(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """Fetch US stocks via Tencent Finance (r_usSYMBOL format)."""
    us = universe[universe.market == "US"]
    us_syms = us["symbol"].tolist()
    tencent_codes = ["r_us" + s for s in us_syms]

    print(f"  US: {len(tencent_codes)} tickers via Tencent Finance ...", flush=True)
    t0 = time.time()
    saved = _tencent_fetch_all(tencent_codes, us_syms, "US", conn, vol_multiplier=1.0)
    print(f"  US Tencent: {saved} rows in {time.time()-t0:.1f}s", flush=True)
    if saved == 0:
        print("  US: Tencent returned 0 rows — trying Yahoo chart direct fallback ...", flush=True)
        saved = _yahoo_batch(us_syms, "US", conn)
    return saved


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--session",
        choices={"morning", "ah_open", "midday", "tail_close", "evening"},
        default=None,
        help="Optional pipeline session for freshness window selection.",
    )
    args = parser.parse_args()

    load_env_file()

    if not UNIVERSE_CSV.exists():
        print(f"[ERROR] {UNIVERSE_CSV} not found — run parse_universe.py first")
        sys.exit(1)

    universe = pd.read_csv(UNIVERSE_CSV)
    print(f"Universe loaded: {len(universe)} stocks")

    conn = _get_conn()
    _prune_old(conn)

    t_total = time.time()
    cn_rows = fetch_cn(universe, conn)
    hk_rows = fetch_hk(universe, conn)
    us_rows = fetch_us(universe, conn)

    expected = {
        "CN": int((universe.market == "CN").sum()),
        "HK": int((universe.market == "HK").sum()),
        "US": int((universe.market == "US").sum()),
    }
    actual = {"CN": cn_rows, "HK": hk_rows, "US": us_rows}
    effective: dict[str, dict[str, object]] = {}
    degraded = []
    for market, exp in expected.items():
        if exp <= 0:
            continue
        accepted_dates = _accepted_trade_dates(market, session=args.session)
        latest_date, covered = _effective_market_coverage(conn, market, accepted_dates)
        ratio = covered / exp
        effective[market] = {
            "accepted_dates": sorted(accepted_dates),
            "latest_trade_date": latest_date,
            "saved": covered,
            "saved_this_run": actual[market],
            "ratio": round(ratio, 4),
        }
        if ratio < MIN_MARKET_COVERAGE_RATIO[market]:
            degraded.append(f"{market}={covered}/{exp} ({ratio:.0%})")

    conn.close()

    elapsed = time.time() - t_total
    write_fetch_manifest(
        {
            "generated_at": datetime.now(_CST).isoformat(),
            "trade_date": _today_cst(),
            "status": "ok" if not degraded else "degraded",
            "coverage": {
                market: {
                    "expected": expected[market],
                    "saved_this_run": actual[market],
                    "saved": int(effective.get(market, {}).get("saved", 0)),
                    "ratio": float(effective.get(market, {}).get("ratio", 0.0)),
                    "latest_trade_date": effective.get(market, {}).get("latest_trade_date"),
                    "accepted_dates": effective.get(market, {}).get("accepted_dates", []),
                }
                for market in ("CN", "HK", "US")
            },
        }
    )
    print(f"\n完成: CN={cn_rows} HK={hk_rows} US={us_rows}  总耗时={elapsed:.0f}s")
    if degraded:
        print(f"[ERROR] Market coverage below threshold: {', '.join(degraded)}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
