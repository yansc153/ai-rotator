from tradingagents.agents.rotation.shortline_enrichment import apply_shortline_enrichment


def test_shortline_enrichment_penalizes_financial_and_insider_warnings():
    item = {
        "symbol": "AIXX",
        "market": "US",
        "horizon": "short",
        "_session_score": 70.0,
        "atr_pct": 0.10,
        "ret_5d": 0.35,
        "fcf_margin": -0.10,
        "m_score": -1.2,
        "z_score": 1.2,
        "form4_cluster_score": 0.8,
        "insider_net_selling_pct": 0.3,
    }

    enriched = apply_shortline_enrichment(
        item,
        session="evening",
        earnings_index={"AIXX": {"symbol": "AIXX", "days_to_earnings": 1}},
        earnings_state="fresh",
    )

    assert "earnings_0_1d" in enriched["event_tags"]
    assert "form4_cluster_selling" in enriched["warning_layer"]
    assert "fcf_penalty" in enriched["warning_layer"]
    assert "m_score_watch" in enriched["warning_layer"]
    assert "z_score_distress" in enriched["warning_layer"]
    assert "high_atr" in enriched["warning_layer"]
    assert enriched["shortline_priority_score"] < item["_session_score"]
