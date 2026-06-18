from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sys
import pandas as pd
from tradingagents.agents.rotation.common import normalize_symbol_for_file


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingagents.agents.rotation.execution_filter import (
    build_freshness_record,
    classify_candidate,
)
import tradingagents.agents.rotation.execution_filter as execution_filter


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


def test_us_freshness_record_uses_15m_file(tmp_path, monkeypatch):
    (tmp_path / "US_NVDA_15m.csv").write_text(
        "datetime,open,high,low,close,volume\n"
        "2026-05-05 15:45:00,1,2,1,2,100\n"
    )
    monkeypatch.setattr(execution_filter, "RAW_DATA_DIR", tmp_path)

    record = build_freshness_record("US", "NVDA", "evening", "2026-05-05")

    assert record.intraday_status == "fresh"
    assert record.source_path.endswith("US_NVDA_15m.csv")


def test_midday_missing_intraday_downgrades_to_watch_only():
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
    assert decision["push_decision"] == "watch_only"
    assert "intraday_missing" in decision["reason_codes"]


def test_non_ai_sector_short_is_rejected_before_watchlist():
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
    assert decision["push_decision"] == "rejected"
    assert "concept_unverified" in decision["reason_codes"]


def test_verified_ai_but_non_active_sector_becomes_watch_only():
    candidate = _base_candidate(active_sector=False, sector="GPU")
    with patch("tradingagents.agents.rotation.execution_filter.build_freshness_record") as mocked:
        mocked.return_value = build_freshness_record("US", "NVDA", "morning", "2026-05-05").model_copy(
            update={"intraday_status": "fresh", "source_path": "/tmp/fresh.csv"}
        )
        decision = classify_candidate(
            candidate,
            session="morning",
            trade_date="2026-05-05",
            active_sector_ids=["AI芯片"],
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


def test_high_atr_warning_downgrades_tradable_candidate():
    candidate = _base_candidate(warning_layer=["high_atr"])
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
    assert "high_atr_watch_only" in decision["reason_codes"]
    assert "high_atr" in decision["invalid_if"]


def test_invalid_three_locks_cannot_bypass_execution_filter():
    candidate = _base_candidate(
        three_locks={
            "status": "invalid",
            "score": 0.0,
            "breakdown_support": True,
        }
    )
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
    assert "three_locks_invalid" in decision["reason_codes"]
    assert "three_locks_support_break" in decision["invalid_if"]


def test_confirmed_three_locks_adds_weight_but_does_not_override_scope():
    candidate = _base_candidate(
        market="CN",
        symbol="688256.SH",
        sector="AI芯片",
        three_locks={"status": "triple_lock", "score": 92.0},
    )
    with patch("tradingagents.agents.rotation.execution_filter.build_freshness_record") as mocked:
        mocked.return_value = build_freshness_record("CN", "688256.SH", "evening", "2026-05-05").model_copy(
            update={"intraday_status": "fresh", "source_path": "/tmp/fresh.csv"}
        )
        decision = classify_candidate(
            candidate,
            session="evening",
            trade_date="2026-05-05",
            active_sector_ids=["AI芯片"],
            earnings_index={},
            earnings_state="absent",
        )

    assert decision["push_decision"] == "rejected"
    assert "market_out_of_scope" in decision["reason_codes"]
    assert decision["execution_score"] < candidate["_session_score"]


def test_midday_uses_15m_intraday_bar_time_cutoff(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    monkeypatch.setattr("tradingagents.agents.rotation.execution_filter.RAW_DATA_DIR", raw_dir)
    symbol = "688256.SH"
    file_key = normalize_symbol_for_file("CN", symbol)
    early = pd.DataFrame(
        [
            {"datetime": "2026-06-18 09:30:00", "open": 12.0, "high": 12.2, "low": 11.8, "close": 11.9, "volume": 1000},
        ]
    )
    early.to_csv(raw_dir / f"CN_{file_key}_15m.csv", index=False)
    record = build_freshness_record("CN", symbol, "midday", "2026-06-18")
    assert record.intraday_status == "stale"

    late = pd.DataFrame(
        [
            {"datetime": "2026-06-18 10:30:00", "open": 12.0, "high": 12.2, "low": 11.8, "close": 11.9, "volume": 1000},
            {"datetime": "2026-06-18 14:05:00", "open": 12.0, "high": 12.8, "low": 11.8, "close": 12.4, "volume": 1200},
        ]
    )
    late.to_csv(raw_dir / f"CN_{file_key}_15m.csv", index=False)
    record = build_freshness_record("CN", symbol, "midday", "2026-06-18")
    assert record.intraday_status == "fresh"


def test_low_market_cap_rejected_even_when_other_gates_are_good():
    candidate = _base_candidate(market="CN", symbol="688256.SH", sector="AI芯片", market_cap=199.0)
    with patch("tradingagents.agents.rotation.execution_filter.build_freshness_record") as mocked:
        mocked.return_value = build_freshness_record("CN", "688256.SH", "midday", "2026-05-05").model_copy(
            update={"intraday_status": "fresh", "source_path": "/tmp/fresh.csv"}
        )
        decision = classify_candidate(
            candidate,
            session="midday",
            trade_date="2026-05-05",
            active_sector_ids=["AI芯片"],
            earnings_index={},
            earnings_state="absent",
        )
    assert decision["push_decision"] == "rejected"
    assert "market_cap_below_200b_cny" in decision["reason_codes"]


def test_trade_language_gate_requires_all_hard_gates():
    candidate = _base_candidate(
        market="CN",
        symbol="688256.SH",
        sector="AI芯片",
        market_cap=250.0,
        three_locks={"status": "double_lock", "score": 66.0, "support_level": 116.0, "pressure_level": 125.0},
    )
    with patch("tradingagents.agents.rotation.execution_filter.build_freshness_record") as mocked:
        mocked.return_value = build_freshness_record("CN", "688256.SH", "midday", "2026-05-05").model_copy(
            update={"intraday_status": "fresh", "source_path": "/tmp/fresh.csv"}
        )
        decision = classify_candidate(
            candidate,
            session="midday",
            trade_date="2026-05-05",
            active_sector_ids=["AI芯片"],
            earnings_index={},
            earnings_state="absent",
        )
    assert decision["trade_language_allowed"] is True
    assert decision["market_board"] == "A股·科创板"
    assert decision["target_plan"]["target_source"] in {"prior_high", "fib_extension"}
