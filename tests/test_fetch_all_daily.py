from datetime import date, datetime, timezone, timedelta
import sqlite3
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_all_daily as daily


def _mk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE daily_prices (
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            pct_change REAL,
            PRIMARY KEY (date, market, symbol)
        )
        """
    )
    return conn


def test_accepted_trade_dates_uses_previous_business_day_for_us():
    monday = datetime(2026, 5, 25, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert daily._accepted_trade_dates("US", monday) == {"2026-05-22"}


def test_effective_market_coverage_accepts_previous_business_day_cache():
    conn = _mk_conn()
    conn.executemany(
        """
        INSERT INTO daily_prices
        (date, market, symbol, open, high, low, close, volume, pct_change)
        VALUES (?, ?, ?, 1, 1, 1, 1, 1, 0)
        """,
        [
            ("2026-05-21", "CN", "000001.SZ"),
            ("2026-05-21", "CN", "000002.SZ"),
        ],
    )
    dates = daily._accepted_trade_dates(
        "CN",
        datetime(2026, 5, 22, 12, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    latest_date, covered = daily._effective_market_coverage(conn, "CN", dates)
    assert latest_date == "2026-05-21"
    assert covered == 2


def test_yf_parse_rows_can_be_mapped_back_to_canonical_hk_symbol():
    columns = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], ["0700.HK"]],
        names=["Price", "Ticker"],
    )
    raw = pd.DataFrame(
        [[500.0, 510.0, 495.0, 505.0, 1000.0]],
        index=pd.to_datetime([date(2026, 5, 22)]),
        columns=columns,
    )
    rows = daily._yf_parse_raw(raw, ["0700.HK"], "HK")
    assert rows[0]["symbol"] == "0700.HK"

    mapped = {"0700.HK": "00700.HK"}
    for row in rows:
        row["symbol"] = mapped.get(row["symbol"], row["symbol"])
    assert rows[0]["symbol"] == "00700.HK"
