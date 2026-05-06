from tradingagents.agents.rotation.price_engine import PriceEngineConfig, build_short_term_plan


def test_long_plan_ok():
    cfg = PriceEngineConfig(long_k1=1.6, long_k2=2.4, long_k3=0.8, min_rr=1.5)
    plan = build_short_term_plan(100, 0.05, "LONG", cfg, market="CN")
    assert plan["rejected"] is False
    assert plan["rr"] >= 1.5


def test_cn_short_rejected():
    plan = build_short_term_plan(100, 0.05, "SHORT", market="CN", short_filters=["overbought", "failed_breakout"])
    assert plan["rejected"] is True
    assert plan["reject_reason"] == "cn_market_no_short"


def test_short_filters_required():
    plan = build_short_term_plan(100, 0.05, "SHORT", market="US", short_filters=["overbought"])
    assert plan["rejected"] is True
    assert plan["reject_reason"] == "short_filter_not_met"


def test_rr_below_threshold_rejected():
    plan = build_short_term_plan(100, 0.04, "LONG", market="US")
    assert plan["rejected"] is True
    assert plan["reject_reason"] == "rr_below_threshold"


def test_us_short_with_two_filters_passes():
    plan = build_short_term_plan(100, 0.05, "SHORT", market="US", short_filters=["overbought", "sector_rollover"])
    assert plan["rejected"] is False
    assert plan["rr"] >= 1.5
