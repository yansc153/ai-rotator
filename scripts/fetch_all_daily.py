"""Batch-fetch daily OHLCV for all 3357 stocks and store in rolling SQLite cache.

Primary data source: Tencent Finance (qt.gtimg.cn) — accessible in CN network,
returns live quotes for CN/HK/US in a single batch HTTP call.

Strategy per market:
  CN  (2276) — Tencent Finance batch (200/call) → today's OHLCV;
                DB already holds rolling 30-day history from prior runs.
                Fallback: akshare spot if Tencent fails.
  HK  ( 702) — Tencent Finance batch (r_hkXXXXX) → today's OHLCV
  US  ( 379) — Tencent Finance batch (r_usSYMBOL) → latest close

NOTE: Yahoo Finance (.SS/.SZ/.HK) and East Money are blocked on CN networks.
      yfinance is kept as a last-resort fallback but will likely time out.

Cache: data/daily_cache.db  (SQLite)
Table: daily_prices(date, market, symbol, open, high, low, close, volume, pct_change)
Keep: last 30 calendar days (auto-pruned on each run)
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _common import PROJECT_ROOT, load_env_file
from tradingagents.runtime import write_fetch_manifest

UNIVERSE_CSV = ROOT / "data" / "universe_full.csv"
DB_PATH = ROOT / "data" / "daily_cache.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

KEEP_DAYS = 30        # rolling window kept in SQLite
YF_CHUNK  = 400       # tickers per yf.download call (CN batches)
YF_CHUNK_SMALL = 50  # smaller batch for US/HK — large batches silently drop many tickers
YF_PERIOD = "30d"     # history window for yfinance
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


def _accepted_trade_dates(market: str, now: datetime | None = None) -> set[str]:
    current = (now or datetime.now(_CST)).astimezone(_CST).date()
    prev = _previous_business_day(current)
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


def _cn_symbol_to_yf(symbol: str) -> str:
    """Convert our CN symbol to yfinance format.

    yfinance uses '.SS' for Shanghai, '.SZ' for Shenzhen (same as ours).
    '600941.SH' → '600941.SS'
    '300308.SZ' → '300308.SZ'   (unchanged)
    '688256.SH' → '688256.SS'   (STAR Market is also Shanghai)
    """
    if symbol.endswith(".SH"):
        return symbol[:-3] + ".SS"
    return symbol  # .SZ unchanged


def _yf_symbol_to_cn(yf_sym: str) -> str:
    """Reverse of _cn_symbol_to_yf — used when mapping results back."""
    if yf_sym.endswith(".SS"):
        return yf_sym[:-3] + ".SH"
    return yf_sym


# ── CN fetch ─────────────────────────────────────────────────────────────────

def fetch_cn(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """Fetch CN stocks via Tencent Finance (primary) + akshare spot (fallback).

    Tencent Finance (qt.gtimg.cn) is accessible from CN networks and returns live
    quotes for all A-share stocks. Yahoo Finance (.SS/.SZ) is blocked and East
    Money is also blocked, so they are attempted only as last-resort fallbacks.

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
        # Fallback: try akshare spot
        print("  CN: Tencent returned 0 rows — trying akshare spot fallback ...", flush=True)
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot()
            cn_symbols_set = set(cn_syms)
            spot_rows = []
            today = _today_cst()
            for _, row in df.iterrows():
                sym = _cn_spot_code_to_symbol(str(row.get("代码", "")))
                if sym is None or sym not in cn_symbols_set:
                    continue
                try:
                    close = float(row.get("最新价", row.get("close", 0)) or 0)
                    if close <= 0:
                        continue
                    spot_rows.append({
                        "date": today, "market": "CN", "symbol": sym,
                        "open":       float(row.get("今开",  close) or close),
                        "high":       float(row.get("最高",  close) or close),
                        "low":        float(row.get("最低",  close) or close),
                        "close":      close,
                        "volume":     float(row.get("成交量", 0) or 0),
                        "pct_change": float(row.get("涨跌幅", 0) or 0),
                    })
                except (ValueError, TypeError):
                    continue
            saved = _upsert(conn, spot_rows)
            print(f"  CN akshare fallback: {saved} rows", flush=True)
        except Exception as exc:
            print(f"  CN akshare fallback FAILED: {exc}", flush=True)

    if saved == 0:
        yf_symbols = [str(v) for v in cn["yf_symbol"].tolist() if str(v)]
        yf_to_sym = {
            str(row["yf_symbol"]): row["symbol"]
            for _, row in cn.iterrows()
            if str(row.get("yf_symbol", "") or "")
        }
        print("  CN: spot fallback returned 0 rows — trying yfinance history fallback ...", flush=True)
        saved = _yf_batch_cn(yf_symbols, yf_to_sym, conn)

    return saved


def _yf_batch_cn(
    yf_tickers: list[str],
    yf_to_sym: dict[str, str],
    conn: sqlite3.Connection,
) -> int:
    """Download CN 30-day history in 400-ticker batches via yfinance."""
    import yfinance as yf

    total = 0
    for i in range(0, len(yf_tickers), YF_CHUNK):
        chunk_yf = yf_tickers[i: i + YF_CHUNK]
        try:
            raw = yf.download(chunk_yf, period=YF_PERIOD, auto_adjust=True,
                              progress=False, threads=True)
        except Exception as exc:
            print(f"  CN yf chunk {i//YF_CHUNK+1}: download error — {exc}", flush=True)
            continue

        if raw.empty:
            continue

        if isinstance(raw.columns, pd.MultiIndex):
            tickers_in = raw.columns.get_level_values(1).unique()
        else:
            raw.columns = pd.MultiIndex.from_tuples(
                [(c, chunk_yf[0]) for c in raw.columns], names=["Price", "Ticker"]
            )
            tickers_in = [chunk_yf[0]]

        rows = []
        for yf_ticker in tickers_in:
            our_sym = yf_to_sym.get(yf_ticker)
            if not our_sym:
                continue
            try:
                sub = raw.xs(yf_ticker, axis=1, level="Ticker").dropna(how="all")
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
                    "date":       str(idx_date)[:10],
                    "market":     "CN",
                    "symbol":     our_sym,
                    "open":       float(drow.get("open",   close)),
                    "high":       float(drow.get("high",   close)),
                    "low":        float(drow.get("low",    close)),
                    "close":      float(close),
                    "volume":     float(drow.get("volume", 0) or 0),
                    "pct_change": round(pct, 4),
                })
        saved = _upsert(conn, rows)
        total += saved
        print(f"  CN yf chunk {i//YF_CHUNK+1}/{(len(yf_tickers)-1)//YF_CHUNK+1}: "
              f"{saved} rows stored", flush=True)
    return total


# ── HK + US: yfinance batch download ─────────────────────────────────────────

def _yf_parse_raw(raw: pd.DataFrame, chunk: list[str], market: str) -> list[dict]:
    """Parse a yfinance download result into row dicts. Handles single and multi-ticker."""
    if raw.empty:
        return []
    if isinstance(raw.columns, pd.MultiIndex):
        tickers_in = raw.columns.get_level_values(1).unique()
    else:
        raw.columns = pd.MultiIndex.from_tuples(
            [(c, chunk[0]) for c in raw.columns], names=["Price", "Ticker"]
        )
        tickers_in = [chunk[0]]

    rows = []
    for ticker in tickers_in:
        resolved_symbol = str(ticker)
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
                "symbol": resolved_symbol,
                "open":   float(drow.get("open",   close)),
                "high":   float(drow.get("high",   close)),
                "low":    float(drow.get("low",    close)),
                "close":  float(close),
                "volume": float(drow.get("volume", 0) or 0),
                "pct_change": round(pct, 4),
            })
    return rows


def _yf_batch(
    tickers: list[str],
    market: str,
    conn: sqlite3.Connection,
    chunk_size: int = YF_CHUNK,
    ticker_to_symbol: dict[str, str] | None = None,
) -> int:
    """Batch-download via yfinance.

    Uses chunk_size tickers per call. After the batch pass, any ticker that ended
    up with <3 rows in the DB is retried individually — this catches tickers that
    yfinance silently drops from large batches (common for small-cap / low-volume names).
    """
    import yfinance as yf

    total = 0
    n_chunks = max(1, (len(tickers) - 1) // chunk_size + 1)
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i: i + chunk_size]
        try:
            raw = yf.download(chunk, period=YF_PERIOD, auto_adjust=True,
                              progress=False, threads=True)
        except Exception as exc:
            print(f"  {market} chunk {i//chunk_size+1}/{n_chunks}: download error — {exc}", flush=True)
            continue
        rows = _yf_parse_raw(raw, chunk, market)
        if ticker_to_symbol:
            for row in rows:
                row["symbol"] = ticker_to_symbol.get(str(row["symbol"]), str(row["symbol"]))
        saved = _upsert(conn, rows)
        total += saved
        print(f"  {market} chunk {i//chunk_size+1}/{n_chunks}: "
              f"{len(chunk)} tickers → {saved} rows stored", flush=True)

    # Retry individual tickers that have <MIN_DAYS rows (silently dropped by batch)
    # Cap at MAX_RETRY_TICKERS and enforce a per-ticker timeout to prevent long-tail hangs.
    import concurrent.futures

    MAX_RETRY_TICKERS = 100   # never spend more than ~17 min on retries
    RETRY_TIMEOUT_S   = 10    # per-ticker network timeout (seconds)

    retry_cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    existing_counts: dict[str, int] = {}
    conn_cursor = conn.execute(
        "SELECT symbol, COUNT(*) FROM daily_prices WHERE market=? AND date>=? GROUP BY symbol",
        (market, retry_cutoff),
    )
    for sym, cnt in conn_cursor.fetchall():
        existing_counts[sym] = cnt

    to_retry = [t for t in tickers if existing_counts.get(t, 0) < 3]
    if to_retry:
        skipped = 0
        if len(to_retry) > MAX_RETRY_TICKERS:
            skipped = len(to_retry) - MAX_RETRY_TICKERS
            to_retry = to_retry[:MAX_RETRY_TICKERS]
        print(
            f"  {market} retry: {len(to_retry)} tickers ({skipped} skipped) "
            f"→ individual download ({RETRY_TIMEOUT_S}s timeout each)",
            flush=True,
        )
        retry_saved = 0
        timed_out = 0

        def _dl_one(ticker: str) -> pd.DataFrame:  # runs in a thread
            return yf.download(
                [ticker], period=YF_PERIOD, auto_adjust=True,
                progress=False, threads=False,
            )

        for ticker in to_retry:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                    _future = _ex.submit(_dl_one, ticker)
                    raw = _future.result(timeout=RETRY_TIMEOUT_S)
                rows = _yf_parse_raw(raw, [ticker], market)
                if ticker_to_symbol:
                    for row in rows:
                        row["symbol"] = ticker_to_symbol.get(str(row["symbol"]), str(row["symbol"]))
                if rows:
                    retry_saved += _upsert(conn, rows)
            except concurrent.futures.TimeoutError:
                timed_out += 1
            except Exception:
                pass

        print(
            f"  {market} retry: {retry_saved} additional rows stored "
            f"({timed_out} tickers timed out)",
            flush=True,
        )
        total += retry_saved

    return total


def fetch_hk(universe: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """Fetch HK stocks via Tencent Finance (r_hkXXXXX format, 5-digit code).

    Uses the akshare_symbol column which already contains the 5-digit HK code.
    """
    hk = universe[universe.market == "HK"].copy()
    # Use akshare_symbol (5-digit, e.g. "00700") when available, else derive from symbol
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
        print("  HK: Tencent returned 0 rows — trying yfinance fallback ...", flush=True)
        saved = _yf_batch(
            yf_symbols,
            "HK",
            conn,
            chunk_size=YF_CHUNK_SMALL,
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
        print("  US: Tencent returned 0 rows — trying yfinance fallback ...", flush=True)
        saved = _yf_batch(us_syms, "US", conn, chunk_size=YF_CHUNK_SMALL)
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
        accepted_dates = _accepted_trade_dates(market)
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
