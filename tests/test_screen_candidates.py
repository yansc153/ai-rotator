from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import screen_candidates as sc


def test_select_candidates_preserves_ambush_and_watch(monkeypatch):
    monkeypatch.setattr(sc, "MARKET_MIN_SLOTS", {"CN": 0, "HK": 0, "US": 0})
    monkeypatch.setattr(sc, "POOL_MIN_SLOTS", {"ambush": 1, "watch": 1})

    candidates = [
        {"symbol": "D1", "market": "CN", "pool": "day_active", "priority_score": 90},
        {"symbol": "D2", "market": "CN", "pool": "day_active", "priority_score": 89},
        {"symbol": "D3", "market": "US", "pool": "day_active", "priority_score": 88},
        {"symbol": "D4", "market": "HK", "pool": "day_active", "priority_score": 87},
        {"symbol": "A1", "market": "US", "pool": "ambush", "priority_score": 40},
        {"symbol": "W1", "market": "HK", "pool": "watch", "priority_score": 30},
    ]

    result = sc._select_candidates_with_diversity(candidates, top_n=4)
    pools = {row["pool"] for row in result}

    assert len(result) == 4
    assert "ambush" in pools
    assert "watch" in pools


def test_select_candidates_falls_back_when_only_day_active_exists(monkeypatch):
    monkeypatch.setattr(sc, "MARKET_MIN_SLOTS", {"CN": 0, "HK": 0, "US": 0})
    monkeypatch.setattr(sc, "POOL_MIN_SLOTS", {"ambush": 1, "watch": 1})

    candidates = [
        {"symbol": "D1", "market": "CN", "pool": "day_active", "priority_score": 90},
        {"symbol": "D2", "market": "CN", "pool": "day_active", "priority_score": 89},
        {"symbol": "D3", "market": "US", "pool": "day_active", "priority_score": 88},
    ]

    result = sc._select_candidates_with_diversity(candidates, top_n=3)

    assert len(result) == 3
    assert all(row["pool"] == "day_active" for row in result)


def test_safe_text_falls_back_on_nan():
    assert sc._safe_text(float("nan"), "POET") == "POET"
    assert sc._safe_text("", "POET") == "POET"
    assert sc._safe_text("  ", "POET") == "POET"
