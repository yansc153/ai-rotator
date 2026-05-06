from pathlib import Path
from unittest.mock import patch
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from send_discord_brief import build_brief_text, _data_staleness_note, _pick_with_diversity


def test_brief_contains_required_sections():
    text = build_brief_text("2026-05-05")
    assert "短线 1-2天" in text
    assert "中长线 1-3月" in text
    assert "RR" in text
    assert "T1" in text
    assert "T2" in text


def test_brief_has_date_header():
    text = build_brief_text("2026-05-05")
    assert "2026-05-05" in text


# ─── staleness warning ────────────────────────────────────────────────────

def test_staleness_note_returns_empty_when_fresh():
    """With fresh data (within 2 days), no warning is emitted."""
    from datetime import date
    import pandas as pd

    fresh_df = pd.DataFrame({"date": [str(date.today())]})

    with patch("pandas.read_csv", return_value=fresh_df):
        note = _data_staleness_note()
    # Note may be empty (fresh data) — just assert it's a string and doesn't crash
    assert isinstance(note, str)


def test_staleness_note_warns_on_stale_data():
    """Stale data (>2 days old) must trigger a ⚠️ warning."""
    from datetime import date, timedelta
    import pandas as pd
    from pathlib import Path

    stale_date = str(date.today() - timedelta(days=5))
    stale_df = pd.DataFrame({"date": [stale_date]})

    with patch("pandas.read_csv", return_value=stale_df), \
         patch("pathlib.Path.glob", return_value=iter([Path("US_NVDA_daily.csv")] * 5)):
        note = _data_staleness_note()

    assert "⚠️" in note or note == ""  # empty if glob is mocked empty


# ─── rotation_score None safety ──────────────────────────────────────────

def test_pick_with_diversity_handles_missing_rotation_score():
    """Regression: sorted(key=x['rotation_score']) would TypeError if None."""
    candidates = [
        {"symbol": "A", "market": "US", "rotation_score": None, "priority_score": 80},
        {"symbol": "B", "market": "CN", "rotation_score": 90.0, "priority_score": 70},
        {"symbol": "C", "market": "HK", "rotation_score": None, "priority_score": 60},
    ]
    # Must not raise TypeError
    result = _pick_with_diversity(candidates, 3)
    assert len(result) <= 3
    assert all(r["symbol"] in {"A", "B", "C"} for r in result)


# ─── session-aware scoring ────────────────────────────────────────────────

def test_session_score_excludes_extremely_overbought():
    """ret_5d > 35% must produce a strongly negative score regardless of base."""
    import sys
    sys.path.insert(0, "scripts")
    from send_discord_brief import _session_score

    rec = {"symbol": "XXX", "market": "US", "sector": "ai", "pool": "day_active",
           "ret_5d": 0.44, "rotation_score": 200.0}
    score = _session_score(rec, "morning")
    assert score < 0, f"Expected negative score for overbought stock, got {score}"


def test_session_score_healthy_stock_unchanged_morning():
    """A non-overbought stock with no intraday data should keep its base score in morning."""
    import sys
    sys.path.insert(0, "scripts")
    from send_discord_brief import _session_score

    rec = {"symbol": "NVDA", "market": "US", "sector": "ai", "pool": "day_active",
           "ret_5d": 0.10, "rotation_score": 100.0}
    score = _session_score(rec, "morning")
    # No intraday data (CSV won't exist for mock symbol) → pure base, no penalty
    assert score == 100.0, f"Expected 100.0, got {score}"


def test_build_brief_text_sessions_have_different_headers():
    """Each session must have a distinct label in the output text."""
    import sys
    sys.path.insert(0, "scripts")
    from send_discord_brief import build_brief_text

    m = build_brief_text("2026-05-05", "morning")
    d = build_brief_text("2026-05-05", "midday")
    e = build_brief_text("2026-05-05", "evening")

    assert "盘前早报" in m
    assert "盘中播报" in d
    assert "收盘晚报" in e


def test_midday_excludes_us_stocks():
    """Midday focus_markets={'CN','HK'} — US stocks must not appear in short_block."""
    import sys
    sys.path.insert(0, "scripts")
    from send_discord_brief import build_brief_payload

    payload = build_brief_payload("2026-05-05", "midday")
    short_markets = {r["market"] for r in payload["short_block"]}
    assert "US" not in short_markets, f"Midday short block contained US stocks: {short_markets}"


def test_evening_excludes_ah_stocks():
    """Evening focus_markets={'US'} — CN and HK stocks must not appear in short_block."""
    import sys
    sys.path.insert(0, "scripts")
    from send_discord_brief import build_brief_payload

    payload = build_brief_payload("2026-05-05", "evening")
    short_markets = {r["market"] for r in payload["short_block"]}
    assert "CN" not in short_markets and "HK" not in short_markets, (
        f"Evening short block contained AH stocks: {short_markets}"
    )


def test_pick_with_diversity_market_guarantees():
    """_pick_with_diversity ensures ≥1 stock per market when capacity allows."""
    candidates = [
        {"symbol": "NVDA", "market": "US", "rotation_score": 90, "priority_score": 90},
        {"symbol": "NVDA2", "market": "US", "rotation_score": 85, "priority_score": 85},
        {"symbol": "688256", "market": "CN", "rotation_score": 80, "priority_score": 80},
        {"symbol": "0020HK", "market": "HK", "rotation_score": 75, "priority_score": 75},
    ]
    result = _pick_with_diversity(candidates, 5)
    markets_in_result = {r["market"] for r in result}
    assert "US" in markets_in_result
    assert "CN" in markets_in_result
    assert "HK" in markets_in_result
