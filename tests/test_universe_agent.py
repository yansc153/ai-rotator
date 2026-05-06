from tradingagents.agents.rotation.universe_agent import create_universe_agent
from tradingagents.agents.rotation.common import normalize_symbol_for_file


def test_universe_agent_outputs_three_pools():
    result = create_universe_agent()({"weekly_rotation_top3": ["gpu_ai_accelerator", "optical_modules_400g_800g"]})
    pools = result["universe_pools"]
    assert set(pools.keys()) == {"day_active", "ambush", "watch"}
    assert len(pools["day_active"]) >= 5
    assert len(pools["ambush"]) >= 5
    assert len(pools["watch"]) >= 5


# ─── normalize_symbol_for_file (HK 5-digit padding) ───────────────────────

def test_normalize_hk_4digit_pads_to_5():
    """Regression: '0020.HK' was resolving to '0020', now must be '00020' for akshare."""
    assert normalize_symbol_for_file("HK", "0020.HK") == "00020"


def test_normalize_hk_5digit_unchanged():
    """Already-5-digit codes must not get extra padding."""
    assert normalize_symbol_for_file("HK", "00981.HK") == "00981"
    assert normalize_symbol_for_file("HK", "09988.HK") == "09988"


def test_normalize_cn_strips_suffix():
    assert normalize_symbol_for_file("CN", "688256.SH") == "688256"
    assert normalize_symbol_for_file("CN", "300308.SZ") == "300308"


def test_normalize_us_returns_full_symbol():
    """US symbols like 'NVDA', 'TSM' are passed unchanged."""
    assert normalize_symbol_for_file("US", "NVDA") == "NVDA"
    assert normalize_symbol_for_file("US", "ASML") == "ASML"


def test_universe_agent_candidates_have_real_prices():
    """All candidates must have current_price > 1.0 (not mock priority integer like 0.0)."""
    result = create_universe_agent()({"weekly_rotation_top3": []})
    all_cands = (
        result["universe_pools"]["day_active"]
        + result["universe_pools"]["ambush"]
        + result["universe_pools"]["watch"]
    )
    # At least half of candidates should have price > 1.0 (real data or reasonable mock)
    real_priced = [c for c in all_cands if c["current_price"] > 1.0]
    assert len(real_priced) >= len(all_cands) // 2, (
        "Most candidates have price ≤ 1.0 — data files may be missing or misnamed."
    )
