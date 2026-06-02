import sqlite3
from pathlib import Path

from tradingagents.agents.rotation.signal_review import (
    build_recent_review_summary,
    record_signals_from_payload,
    refresh_signal_outcomes,
    signal_rows_from_payload,
)


def _daily_cache(path: Path) -> None:
    with sqlite3.connect(path) as conn:
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
        conn.executemany(
            """
            INSERT INTO daily_prices
            (date, market, symbol, open, high, low, close, volume, pct_change)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-06-01", "US", "NVDA", 100.0, 104.0, 98.0, 102.0, 1000, 0.02),
                ("2026-06-02", "US", "NVDA", 102.0, 112.0, 101.0, 110.0, 1200, 0.08),
                ("2026-06-03", "US", "NVDA", 110.0, 115.0, 108.0, 114.0, 1300, 0.04),
            ],
        )


def _payload() -> dict:
    return {
        "run_id": "run-1",
        "date": "2026-06-01",
        "session": "evening",
        "opportunity_buckets": {
            "premarket_open_sell": [
                {
                    "symbol": "NVDA",
                    "market": "US",
                    "company_name": "NVIDIA",
                    "sector": "GPU",
                    "trade_style": "盘前强势",
                    "current_price": 100.0,
                    "execution_score": 80.0,
                    "reason": "三把锁确认 + 赛道轮动",
                    "three_locks": {
                        "status": "triple_lock",
                        "score": 90.0,
                        "support_level": 96.0,
                        "pressure_level": 108.0,
                    },
                }
            ],
            "intraday_dip_reversal": [],
            "overheat_failure_short": [],
            "radar_watch": [],
        },
    }


def test_signal_rows_from_payload_keeps_three_locks_and_push_price():
    rows = signal_rows_from_payload(_payload())

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "NVDA"
    assert row["playbook"] == "premarket_open_sell"
    assert row["side"] == "LONG"
    assert row["push_price"] == 100.0
    assert row["three_locks_status"] == "triple_lock"
    assert row["support_level"] == 96.0


def test_refresh_signal_outcomes_uses_push_price_to_current_price(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROTATOR_DB_PATH", str(tmp_path / "runtime.db"))
    cache_path = tmp_path / "daily_cache.db"
    _daily_cache(cache_path)

    record_signals_from_payload(_payload())
    outcomes = refresh_signal_outcomes(review_date="2026-06-03", cache_path=cache_path)
    summary = build_recent_review_summary(review_date="2026-06-03", days=3)

    assert len(outcomes) == 1
    assert outcomes[0]["current_price"] == 114.0
    assert round(outcomes[0]["raw_return_pct"], 4) == 0.14
    assert round(outcomes[0]["max_gain_pct"], 4) == 0.15
    assert round(outcomes[0]["max_drawdown_pct"], 4) == -0.02
    assert summary["signal_count"] == 1
    assert summary["priced_count"] == 1
    assert round(summary["avg_raw_return_pct"], 4) == 0.14
