from __future__ import annotations

import pandas as pd

from tradingagents.agents.rotation.three_locks import evaluate_three_locks


def _frame(values: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=len(values), freq="D")
    rows = []
    for idx, close in enumerate(values):
        rows.append(
            {
                "date": str(dates[idx].date()),
                "open": close * 0.99,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1_000_000 + idx,
            }
        )
    return pd.DataFrame(rows)


def test_three_locks_requires_enough_history():
    result = evaluate_three_locks(_frame([10.0] * 10))

    assert result["status"] == "insufficient_history"
    assert result["score"] == 0.0
    assert result["support_level"] is None
    assert result["pressure_level"] is None


def test_three_locks_returns_explainable_contract_for_valid_history():
    values = [10 + idx * 0.08 for idx in range(20)]
    result = evaluate_three_locks(_frame(values))

    assert result["status"] in {"triple_lock", "double_lock", "single_lock", "invalid"}
    assert isinstance(result["score"], float)
    assert isinstance(result["above_ma5"], bool)
    assert isinstance(result["above_ma10"], bool)
    assert "reason" in result and result["reason"]
    assert "support_level" in result
    assert "pressure_level" in result


def test_three_locks_marks_support_break_as_invalid():
    values = [10 + idx * 0.05 for idx in range(70)] + [8.0, 7.5, 7.0, 6.5, 6.0]
    result = evaluate_three_locks(_frame(values))

    assert result["status"] == "invalid"
    assert result["breakdown_support"] is True
