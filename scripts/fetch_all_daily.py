"""Batch-fetch daily OHLCV for all 3357 stocks and store in rolling SQLite cache.

Strategy per market:
  CN  (2276) — ak.stock_zh_a_spot()  : one call, returns today's full A-share snapshot
                                        (~18s, all exchange codes in sh/sz prefix format)
  HK  ( 702) — yf.download(batch)   : one call per ~400-ticker chunk, 25 days history
  US  ( 379) — yf.download(batch)   : one call, 25 days history

Cache: data/daily_cache.db  (SQLite)
Table: daily_prices(date, market, symbol, open, high, low, close, volume, pct_change)
Keep: last 30 calendar days (auto-pruned on each run)
"""
from __future__ import annotations

import sqlite3
import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _common import PROJECT_ROOT, load_env_file

UNIVERSE_CSV = ROOT / "data" / "universe_full.csv"
DB_PATH = ROOT / "data" / "daily_cache.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

KEEP_DAYS = 30        # rolling window kept in SQLite
YF_CHUNK  = 400       # tickers per yf.download call
YF_PERIOD = "30d"     # history window for yfinance
warnings.filterwarnings("ignore")


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


# ── CN: akshare spot (one call, full market) ─────────────────────────────────

def _cn_spot_code_to_symbol(code: str) -> str | None:
    """Convert akshare spot code to our standard symbol.

    'sh600941' → '600941.SH'
    'sz300308' → '300308.SZ'
    'bj920000' → None  (Beijing exchange — skip)
    """
    code = code.lower()
    if code.startswith("sh"):
        return f"{code[2:]}.SH"
    if code.startswith("sz"):
        return f"{code[2:]}.SZ"
    return None  # BJ or unknown


def fetch_cn(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    import akshare as ak

    cn_symbols = set(universe[universe.market == "CN"]["symbol"])
    today = date.today().isoformat()

    print("  CN: calling ak.stock_zh_a_spot() ...", flush=True)
    t0 = time.time()
    df = ak.stock_zh_a_spot()
    print(f"  CN: {len(df)} rows fetched in {time.time()-t0:.1f}s", flush=True)

    rows = []
    for _, row in df.iterrows():
        sym = _cn_spot_code_to_symbol(str(row.get("代码", "")))
        if sym is None or sym not in cn_symbols:
            continue
        try:
            rows.append({
                "date": today, "market": "CN", "symbol": sym,
                "open":   float(row.get("今开",  row.get("open",  0)) or 0),
                "high":   float(row.get("最高",  row.get("high",  0)) or 0),
                "low":    float(row.get("最低",  row.get("low",   0)) or 0),
                "close":  float(row.get("最新价", row.get("close", 0)) or 0),
                "volume": float(row.get("成交量", row.get("volume", 0)) or 0),
                "pct_change": float(row.get("涨跌幅", 0) or 0),
            })
        except (ValueError, TypeError):
            continue

    saved = _upsert(conn, rows)
    print(f"  CN: {saved}/{len(cn_symbols)} symbols stored", flush=True)
    return saved


# ── HK + US: yfinance batch download ─────────────────────────────────────────

def _yf_batch(tickers: list[str], market: str, conn: sqlite3.Connection) -> int:
    import yfinance as yf

    total = 0
    for i in range(0, len(tickers), YF_CHUNK):
        chunk = tickers[i: i + YF_CHUNK]
        try:
            raw = yf.download(chunk, period=YF_PERIOD, auto_adjust=True,
                              progress=False, threads=True)
        except Exception as exc:
            print(f"  {market} chunk {i//YF_CHUNK+1}: download error — {exc}", flush=True)
            continue

        if raw.empty:
            continue

        # yfinance returns MultiIndex columns (Price, Ticker) when >1 ticker
        if isinstance(raw.columns, pd.MultiIndex):
            tickers_in = raw.columns.get_level_values(1).unique()
        else:
            # Single ticker — wrap for uniform handling
            raw.columns = pd.MultiIndex.from_tuples(
                [(c, chunk[0]) for c in raw.columns], names=["Price", "Ticker"]
            )
            tickers_in = [chunk[0]]

        rows = []
        for ticker in tickers_in:
            try:
                sub = raw.xs(ticker, axis=1, level="Ticker").dropna(how="all")
            except KeyError:
                continue
            sub = sub.rename(columns=str.lower)
            for idx_date, drow in sub.iterrows():
                close = drow.get("close")
                if pd.isna(close) or close <= 0:
                    continue
                prev_idx = sub.index.get_loc(idx_date) - 1
                prev_close = sub.iloc[prev_idx]["close"] if prev_idx >= 0 else close
                pct = (close - prev_close) / prev_close * 100 if prev_close else 0
                rows.append({
                    "date": str(idx_date)[:10],
                    "market": market,
                    "symbol": ticker,
                    "open":   float(drow.get("open",   close)),
                    "high":   float(drow.get("high",   close)),
                    "low":    float(drow.get("low",    close)),
                    "close":  float(close),
                    "volume": float(drow.get("volume", 0) or 0),
                    "pct_change": round(pct, 4),
                })
        saved = _upsert(conn, rows)
        total += saved
        print(f"  {market} chunk {i//YF_CHUNK+1}/{(len(tickers)-1)//YF_CHUNK+1}: "
              f"{saved} rows stored", flush=True)
    return total


def fetch_hk(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    hk = universe[universe.market == "HK"]
    yf_tickers = hk["yf_symbol"].dropna().unique().tolist()
    print(f"  HK: {len(yf_tickers)} tickers via yf.download ...", flush=True)
    t0 = time.time()
    saved = _yf_batch(yf_tickers, "HK", conn)
    print(f"  HK: done in {time.time()-t0:.1f}s  total rows={saved}", flush=True)
    return saved


def fetch_us(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    us = universe[universe.market == "US"]
    tickers = us["yf_symbol"].dropna().unique().tolist()
    print(f"  US: {len(tickers)} tickers via yf.download ...", flush=True)
    t0 = time.time()
    saved = _yf_batch(tickers, "US", conn)
    print(f"  US: done in {time.time()-t0:.1f}s  total rows={saved}", flush=True)
    return saved


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
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
    conn.close()

    elapsed = time.time() - t_total
    print(f"\n完成: CN={cn_rows} HK={hk_rows} US={us_rows}  总耗时={elapsed:.0f}s")


if __name__ == "__main__":
    main()
