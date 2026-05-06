"""
Tests for reviewer_agent — focused on fixes from May 2026 audit:
  - Real T+1 date arithmetic (next business day, swing calendar days)
  - Review horizon gating (only review if review_date <= today)
  - PnL calculation correctness
  - Already-reviewed skip logic
  - HK 5-digit zero-padding in fetch path
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch, MagicMock

from tradingagents.agents.rotation.reviewer_agent import (
    _next_business_day,
    _fetch_outcome_price,
    HORIZON_REVIEW_DAYS,
)


# ─── _next_business_day ────────────────────────────────────────────────────

def test_next_business_day_mon_to_tue():
    assert _next_business_day(date(2026, 5, 4), 1) == date(2026, 5, 5)  # Mon → Tue


def test_next_business_day_fri_to_mon():
    """Friday +1 business day = Monday, not Saturday."""
    assert _next_business_day(date(2026, 5, 8), 1) == date(2026, 5, 11)  # Fri → Mon


def test_next_business_day_fri_two_days():
    """Friday +2 business days = Tuesday."""
    assert _next_business_day(date(2026, 5, 8), 2) == date(2026, 5, 12)  # Fri → Tue


def test_next_business_day_skips_weekend():
    """Saturday +1 business day = Monday."""
    assert _next_business_day(date(2026, 5, 9), 1) == date(2026, 5, 11)  # Sat → Mon


# ─── Horizon constants ─────────────────────────────────────────────────────

def test_horizon_review_days_short():
    assert HORIZON_REVIEW_DAYS["short"] == 1


def test_horizon_review_days_swing():
    assert HORIZON_REVIEW_DAYS["swing"] == 30


# ─── _fetch_outcome_price HK 5-digit padding ──────────────────────────────

def test_fetch_outcome_price_hk_uses_5digit_code():
    """Regression: akshare needs '00020' not '0020' for SenseTime. Verify zfill(5) applied."""
    import akshare as ak
    captured = {}

    def fake_stock_hk_daily(symbol, adjust):
        captured["symbol"] = symbol
        # Return empty df to simulate no data for this date (we only care about the symbol passed)
        import pandas as pd
        return pd.DataFrame()

    with patch.object(ak, "stock_hk_daily", side_effect=fake_stock_hk_daily):
        _fetch_outcome_price("HK", "0020.HK", "2026-05-05")

    assert captured.get("symbol") == "00020", (
        f"Expected 5-digit '00020', got '{captured.get('symbol')}'. "
        "fetch_hk must zero-pad to 5 digits for akshare."
    )


def test_fetch_outcome_price_hk_5digit_already_padded():
    """5-digit symbols like '00981.HK' must stay '00981' (no extra padding)."""
    import akshare as ak
    captured = {}

    def fake_stock_hk_daily(symbol, adjust):
        captured["symbol"] = symbol
        import pandas as pd
        return pd.DataFrame()

    with patch.object(ak, "stock_hk_daily", side_effect=fake_stock_hk_daily):
        _fetch_outcome_price("HK", "00981.HK", "2026-05-05")

    assert captured.get("symbol") == "00981"


# ─── _fetch_outcome_price returns None on failure ─────────────────────────

def test_fetch_outcome_price_us_returns_none_on_empty():
    import yfinance as yf
    import pandas as pd

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()

    with patch.object(yf, "Ticker", return_value=mock_ticker):
        result = _fetch_outcome_price("US", "NVDA", "2026-05-05")

    assert result is None


def test_fetch_outcome_price_returns_none_on_exception():
    import yfinance as yf
    mock_ticker = MagicMock()
    mock_ticker.history.side_effect = RuntimeError("network error")

    with patch.object(yf, "Ticker", return_value=mock_ticker):
        result = _fetch_outcome_price("US", "NVDA", "2026-05-05")

    assert result is None


def test_fetch_outcome_price_us_returns_close():
    import yfinance as yf
    import pandas as pd

    mock_df = pd.DataFrame(
        {"Close": [196.50], "Open": [199.0], "High": [200.0], "Low": [195.0]},
        index=pd.to_datetime(["2026-05-05"]),
    )
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_df

    with patch.object(yf, "Ticker", return_value=mock_ticker):
        result = _fetch_outcome_price("US", "NVDA", "2026-05-05")

    assert result == 196.50


# ─── Reviewer node logic ──────────────────────────────────────────────────

def test_reviewer_skips_future_review_dates():
    """Recommendations with review_date > today must be skipped (not reviewed early)."""
    from tradingagents.agents.rotation.reviewer_agent import create_reviewer_agent
    from datetime import date

    future_trade_date = str(date.today())  # trade_date=today → T+1 review date = tomorrow

    fake_recs = [
        {
            "id": 1,
            "trade_date": future_trade_date,
            "horizon": "short",
            "market": "US",
            "symbol": "NVDA",
            "current_price": 196.50,
            "stop_loss": 186.0,
        }
    ]

    with patch("tradingagents.agents.rotation.reviewer_agent.list_recommendations", return_value=fake_recs), \
         patch("tradingagents.agents.rotation.reviewer_agent.list_reviewed_recommendation_ids", return_value=set()), \
         patch("tradingagents.agents.rotation.reviewer_agent.insert_outcomes") as mock_insert:
        node = create_reviewer_agent()
        result = node({"trade_date": str(date.today())})

    assert result["review_outcomes"] == []  # T+1 not yet due
    mock_insert.assert_called_once_with([])  # nothing written


def test_reviewer_skips_already_reviewed():
    """Recommendations already in outcomes table must not be re-reviewed."""
    from tradingagents.agents.rotation.reviewer_agent import create_reviewer_agent
    from datetime import date, timedelta

    old_trade_date = str(date.today() - timedelta(days=3))
    fake_recs = [{"id": 99, "trade_date": old_trade_date, "horizon": "short",
                  "market": "US", "symbol": "NVDA", "current_price": 200.0, "stop_loss": 190.0}]

    with patch("tradingagents.agents.rotation.reviewer_agent.list_recommendations", return_value=fake_recs), \
         patch("tradingagents.agents.rotation.reviewer_agent.list_reviewed_recommendation_ids", return_value={99}), \
         patch("tradingagents.agents.rotation.reviewer_agent.insert_outcomes") as mock_insert:
        node = create_reviewer_agent()
        result = node({"trade_date": str(date.today())})

    assert result["review_outcomes"] == []
    mock_insert.assert_called_once_with([])


def test_reviewer_computes_pnl_correctly():
    """pnl_pct = (close - entry) / entry, thesis_valid=0 if breaches stop."""
    from tradingagents.agents.rotation.reviewer_agent import create_reviewer_agent
    from datetime import date, timedelta
    import yfinance as yf
    import pandas as pd

    old_date = str(date.today() - timedelta(days=3))
    fake_recs = [{"id": 5, "trade_date": old_date, "horizon": "short",
                  "market": "US", "symbol": "NVDA", "current_price": 200.0, "stop_loss": 190.0}]

    # close=180 → pnl = (180-200)/200 = -10%, stop_pct = (200-190)/200 = 5% → breach → thesis_valid=0
    mock_df = pd.DataFrame(
        {"Close": [180.0], "Open": [195.0], "High": [196.0], "Low": [179.0]},
        index=pd.to_datetime([str(date.today() - timedelta(days=2))]),
    )
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_df

    with patch("tradingagents.agents.rotation.reviewer_agent.list_recommendations", return_value=fake_recs), \
         patch("tradingagents.agents.rotation.reviewer_agent.list_reviewed_recommendation_ids", return_value=set()), \
         patch("tradingagents.agents.rotation.reviewer_agent.insert_outcomes") as mock_insert, \
         patch.object(yf, "Ticker", return_value=mock_ticker):
        node = create_reviewer_agent()
        result = node({"trade_date": str(date.today())})

    outcomes = result["review_outcomes"]
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["pnl_pct"] == round((180.0 - 200.0) / 200.0, 6)
    assert o["thesis_valid"] == 0
    assert o["failure_layer"] == "price"
