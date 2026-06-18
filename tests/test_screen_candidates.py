from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import screen_candidates as sc
from tradingagents.agents.rotation.company_concept import ashare_board, market_cap_gate, verify_company_concept


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


def test_allowed_latest_dates_prefers_manifest_market_dates():
    manifest = {
        "coverage": {
            "CN": {"accepted_dates": ["2026-05-25", "2026-05-22"]},
            "US": {"accepted_dates": ["2026-05-22"]},
        }
    }

    assert sc._allowed_latest_dates("CN", manifest) == {"2026-05-25", "2026-05-22"}
    assert sc._allowed_latest_dates("US", manifest) == {"2026-05-22"}


def test_allowed_latest_dates_falls_back_without_manifest():
    dates = sc._allowed_latest_dates("US", None)
    assert len(dates) == 2


def test_ashare_board_classification_covers_required_prefixes():
    assert ashare_board("000001.SZ", "CN") == "深主板"
    assert ashare_board("002972.SZ", "CN") == "深主板"
    assert ashare_board("300750.SZ", "CN") == "创业板"
    assert ashare_board("301000.SZ", "CN") == "创业板"
    assert ashare_board("600519.SH", "CN") == "沪主板"
    assert ashare_board("603986.SH", "CN") == "沪主板"
    assert ashare_board("688256.SH", "CN") == "科创板"
    assert ashare_board("689009.SH", "CN") == "科创板"


def test_market_cap_gate_rejects_below_missing_and_unknown_currency():
    assert market_cap_gate("CN", 199.99)["market_cap_ok"] is False
    assert market_cap_gate("CN", None)["market_cap_ok"] is False
    assert market_cap_gate("XX", 9999)["market_cap_ok"] is False
    assert market_cap_gate("US", 3.0)["market_cap_ok"] is True


def test_fenghua_gaoke_mlcc_is_not_trade_verified_ai():
    concept = verify_company_concept(
        {
            "symbol": "000636.SZ",
            "company_name": "风华高科",
            "sector": "MLCC",
            "sector_tags": "MLCC;被动元件",
            "chain_group": "电子元件",
        },
        evidence_date="2026-06-18",
    )
    assert concept["concept_verified"] is False
    assert concept["company_concept"] == "MLCC/被动元件"
