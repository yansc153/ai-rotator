from tradingagents.agents.rotation.bottleneck_scout import build_bottleneck_block


def _config() -> dict:
    return {
        "themes": [
            {
                "theme_id": "ai_power",
                "theme": "AI电源",
                "bottleneck_cn": "高压功率瓶颈",
                "scarcity": "high",
                "symbols": [
                    {
                        "symbol": "WOLF",
                        "market": "US",
                        "company_name": "Wolfspeed",
                        "sector": "碳化硅",
                        "chain_role": "SiC supply",
                        "why_buy": "AI机柜高压化提高SiC需求。",
                        "hold_reason": "持有到客户认证和产能利用率继续验证。",
                        "irreplaceable_role": "提供SiC材料和器件。",
                        "conviction": 70,
                        "evidence": ["Serenity点名", "本地候选池出现"],
                        "watch_triggers": ["800VDC继续被验证"],
                        "invalid_if": ["融资风险压过需求"],
                    },
                    {
                        "symbol": "300308.SZ",
                        "market": "CN",
                        "company_name": "中际旭创",
                        "sector": "光模块",
                        "chain_role": "optical module",
                        "why_buy": "A股光模块主线确认器。",
                        "hold_reason": "持有到订单和业绩继续兑现。",
                        "irreplaceable_role": "海外AI互连需求的A股映射。",
                        "conviction": 65,
                    },
                ],
            }
        ]
    }


def test_bottleneck_block_scores_and_preserves_hold_thesis():
    source_items = [
        {
            "symbol": "WOLF",
            "market": "US",
            "company_name": "Wolfspeed",
            "sector": "碳化硅",
            "pool": "day_active",
            "current_price": 69.5,
            "ret_5d": 0.12,
            "ret_20d": 0.40,
            "rotation_score": 50.0,
        }
    ]

    block = build_bottleneck_block(
        source_items,
        session="evening",
        limit=3,
        focus_markets={"US"},
        config=_config(),
    )

    assert [item["symbol"] for item in block] == ["WOLF"]
    assert block[0]["horizon"] == "bottleneck"
    assert block[0]["push_decision"] == "watch_only"
    assert block[0]["bottleneck_score"] > 70
    assert "客户认证" in block[0]["hold_reason"]
    assert block[0]["current_price"] == 69.5
    assert block[0]["source_pool"] == "day_active"
    assert block[0]["source_reason"] == "本轮扫描命中:day_active"


def test_bottleneck_block_marks_untracked_static_research_honestly():
    config = {
        "themes": [
            {
                "theme_id": "optical",
                "theme": "光互连",
                "symbols": [
                    {
                        "symbol": "SIVE_TEST",
                        "market": "US",
                        "company_name": "Sivers Test",
                        "sector": "CW laser",
                        "conviction": 80,
                    }
                ],
            }
        ]
    }

    block = build_bottleneck_block(
        [],
        session="evening",
        limit=1,
        focus_markets={"US"},
        config=config,
    )

    assert block[0]["source_pool"] == "untracked_static_watchlist"
    assert "尚未接入" in block[0]["source_reason"]


def test_bottleneck_block_respects_focus_markets():
    block = build_bottleneck_block(
        [],
        session="ah_open",
        limit=5,
        focus_markets={"CN"},
        config=_config(),
    )

    assert [item["symbol"] for item in block] == ["300308.SZ"]
