from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingagents.agents.rotation.execution_filter import (
    build_freshness_record,
    classify_candidate,
)


def _base_candidate(**overrides):
    base = {
        "symbol": "NVDA",
        "market": "US",
        "sector": "GPU",
        "pool": "day_active",
        "horizon": "short",
        "rotation_score": 90.0,
        "priority_score": 90.0,
        "_session_score": 90.0,
        "current_price": 900.0,
        "market_cap": 2500.0,
        "active_sector": True,
        "rank_in_sector": 1,
        "sector_fit_score": 100.0,
    }
    base.update(overrides)
    return base


def test_build_freshness_record_missing_file():
    record = build_freshness_record("US", "NVDA", "midday", "2026-05-05")
    assert record.intraday_status in {"missing", "fresh", "stale", "failed"}


def test_midday_missing_intraday_does_not_block_when_session_snapshot_is_fresh():
    candidate = _base_candidate(market="CN", symbol="688256.SH", sector="AI芯片")
    with patch("tradingagents.agents.rotation.execution_filter.build_freshness_record") as mocked:
        mocked.return_value = build_freshness_record("CN", "688256.SH", "midday", "2026-05-05").model_copy(
            update={"intraday_status": "missing", "source_path": "/tmp/missing.csv"}
        )
        decision = classify_candidate(
            candidate,
            session="midday",
            trade_date="2026-05-05",
            active_sector_ids=["AI芯片"],
            earnings_index={},
            earnings_state="absent",
        )
    assert decision["push_decision"] == "tradable_now"
    assert "intraday_missing" not in decision["reason_codes"]


def test_non_active_sector_short_becomes_watch_only():
    candidate = _base_candidate(active_sector=False, sector="OTHER")
    with patch("tradingagents.agents.rotation.execution_filter.build_freshness_record") as mocked:
        mocked.return_value = build_freshness_record("US", "NVDA", "morning", "2026-05-05").model_copy(
            update={"intraday_status": "fresh", "source_path": "/tmp/fresh.csv"}
        )
        decision = classify_candidate(
            candidate,
            session="morning",
            trade_date="2026-05-05",
            active_sector_ids=["GPU"],
            earnings_index={},
            earnings_state="absent",
        )
    assert decision["push_decision"] == "watch_only"
    assert "not_in_active_sector" in decision["reason_codes"]


def test_evening_data_limited_catalyst_not_tradable_now():
    candidate = _base_candidate()
    with patch("tradingagents.agents.rotation.execution_filter.build_freshness_record") as mocked:
        mocked.return_value = build_freshness_record("US", "NVDA", "evening", "2026-05-05").model_copy(
            update={"intraday_status": "fresh", "source_path": "/tmp/fresh.csv"}
        )
        decision = classify_candidate(
            candidate,
            session="evening",
            trade_date="2026-05-05",
            active_sector_ids=["GPU"],
            earnings_index={"NVDA": {"symbol": "NVDA", "data_limited": True}},
            earnings_state="fresh",
        )
    assert decision["push_decision"] == "watch_only"
    assert "catalyst_data_limited" in decision["reason_codes"]
