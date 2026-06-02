"""Backfill 30-day daily OHLCV history through yfinance into daily_cache.db.

The normal daily fetch path prefers Tencent Finance because it is fast and
works reliably for same-day broad scans. This script is for fresh hosts or cache
repair: force yfinance history downloads so confirmation layers such as ATR and
three_locks have enough bars before the daily timers run.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_all_daily as daily
from _common import load_env_file


DEFAULT_MARKETS = ("CN", "HK", "US")


def _market_tickers(universe: pd.DataFrame, market: str) -> tuple[list[str], dict[str, str]]:
    rows = universe[universe.market == market]
    tickers: list[str] = []
    ticker_to_symbol: dict[str, str] = {}
    for _, row in rows.iterrows():
        symbol = str(row.get("symbol", "") or "").strip()
        yf_symbol = str(row.get("yf_symbol", "") or "").strip()
        ticker = yf_symbol or symbol
        if not symbol or not ticker:
            continue
        tickers.append(ticker)
        ticker_to_symbol[ticker] = symbol
    return tickers, ticker_to_symbol


def _coverage(conn: sqlite3.Connection, min_bars: int) -> list[tuple[str, int, int, str | None, str | None]]:
    cutoff = (date.today() - timedelta(days=daily.KEEP_DAYS)).isoformat()
    return conn.execute(
        """
        SELECT market,
               COUNT(*) AS symbols,
               SUM(CASE WHEN bars >= ? THEN 1 ELSE 0 END) AS ready_symbols,
               MIN(first_date) AS first_date,
               MAX(last_date) AS last_date
        FROM (
            SELECT market, symbol, COUNT(*) AS bars, MIN(date) AS first_date, MAX(date) AS last_date
            FROM daily_prices
            WHERE date >= ?
            GROUP BY market, symbol
        )
        GROUP BY market
        ORDER BY market
        """,
        (min_bars, cutoff),
    ).fetchall()


def _ready_symbols(conn: sqlite3.Connection, market: str, min_bars: int) -> set[str]:
    cutoff = (date.today() - timedelta(days=daily.KEEP_DAYS)).isoformat()
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT symbol
            FROM daily_prices
            WHERE market=? AND date >= ?
            GROUP BY symbol
            HAVING COUNT(*) >= ?
            """,
            (market, cutoff, min_bars),
        )
    }


def _filter_tickers_needing_backfill(
    tickers: list[str],
    ticker_to_symbol: dict[str, str],
    ready_symbols: set[str],
) -> list[str]:
    return [ticker for ticker in tickers if ticker_to_symbol.get(ticker, ticker) not in ready_symbols]


def backfill(markets: list[str], chunk_size: int, min_bars: int) -> None:
    load_env_file()
    universe_path = daily.UNIVERSE_CSV
    if not universe_path.exists():
        print(f"[ERROR] {universe_path} not found — run parse_universe.py first", flush=True)
        raise SystemExit(1)

    universe = pd.read_csv(universe_path)
    conn = daily._get_conn()
    daily._prune_old(conn)

    started = time.time()
    totals: dict[str, int] = {}
    for market in markets:
        tickers, ticker_to_symbol = _market_tickers(universe, market)
        if not tickers:
            print(f"{market}: no tickers", flush=True)
            totals[market] = 0
            continue
        ready = _ready_symbols(conn, market, min_bars)
        original_count = len(tickers)
        tickers = _filter_tickers_needing_backfill(tickers, ticker_to_symbol, ready)
        skipped = original_count - len(tickers)
        if not tickers:
            print(f"{market}: all {original_count} tickers already have >= {min_bars} bars", flush=True)
            totals[market] = 0
            continue
        print(
            f"{market}: backfilling {len(tickers)} tickers via yfinance 30d "
            f"(skipping {skipped} already ready) ...",
            flush=True,
        )
        saved = daily._yf_batch(
            tickers,
            market,
            conn,
            chunk_size=chunk_size,
            ticker_to_symbol=ticker_to_symbol if market in {"CN", "HK"} else None,
        )
        totals[market] = saved

    print("\nBackfill saved rows:", " ".join(f"{m}={totals.get(m, 0)}" for m in markets), flush=True)
    for market, symbols, ready, first_date, last_date in _coverage(conn, min_bars):
        print(
            f"Coverage {market}: {ready}/{symbols} symbols >= {min_bars} bars "
            f"({first_date} → {last_date})",
            flush=True,
        )
    conn.close()
    print(f"Done in {time.time() - started:.0f}s", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill 30-day yfinance history into daily_cache.db")
    parser.add_argument(
        "--markets",
        default=",".join(DEFAULT_MARKETS),
        help="Comma-separated markets to backfill: CN,HK,US",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(os.getenv("AI_ROTATOR_BACKFILL_CHUNK_SIZE", "50")),
        help="yfinance batch size",
    )
    parser.add_argument(
        "--min-bars",
        type=int,
        default=14,
        help="Readiness threshold for coverage reporting",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    invalid = [m for m in markets if m not in DEFAULT_MARKETS]
    if invalid:
        print(f"[ERROR] invalid markets: {', '.join(invalid)}", flush=True)
        raise SystemExit(2)
    backfill(markets, args.chunk_size, args.min_bars)


if __name__ == "__main__":
    main()
