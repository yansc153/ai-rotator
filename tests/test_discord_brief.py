import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import send_discord_brief as brief_module
from send_discord_brief import build_brief_payload, build_brief_text, _data_staleness_note, _pick_with_diversity


def _fake_rotation_payloads() -> dict[str, dict]:
    us_short = {
        "symbol": "NVDA",
        "company_name": "NVIDIA",
        "market": "US",
        "sector": "GPU",
        "pool": "day_active",
        "horizon": "short",
        "rotation_score": 90.0,
        "priority_score": 90.0,
        "three_locks": {"status": "triple_lock", "score": 92.0, "support_level": 880.0, "pressure_level": 930.0, "reason": "红K + 站上操盘线"},
        "ret_5d": 0.10,
        "current_price": 900.0,
        "market_cap": 2500.0,
        "plan": {"entry_low": 890.0, "entry_high": 905.0, "target_1": 940.0, "target_2": 970.0, "stop_loss": 860.0, "rr": 2.0},
        "thesis": "GPU leader",
    }
    cn_short = {
        "symbol": "688256.SH",
        "company_name": "寒武纪",
        "market": "CN",
        "sector": "AI芯片",
        "pool": "day_active",
        "horizon": "short",
        "rotation_score": 84.0,
        "priority_score": 84.0,
        "three_locks": {"status": "double_lock", "score": 66.0, "support_level": 116.0, "pressure_level": 125.0, "reason": "红K + 站上黄金线"},
        "ret_5d": 0.08,
        "current_price": 120.0,
        "market_cap": 50.0,
        "plan": {"entry_low": 118.0, "entry_high": 121.0, "target_1": 126.0, "target_2": 132.0, "stop_loss": 114.0, "rr": 2.0},
        "thesis": "CN AI chip momentum",
    }
    hk_short = {
        "symbol": "0020.HK",
        "company_name": "商汤",
        "market": "HK",
        "sector": "AI应用",
        "pool": "day_active",
        "horizon": "short",
        "rotation_score": 78.0,
        "priority_score": 78.0,
        "three_locks": {"status": "single_lock", "score": 36.0, "support_level": 1.68, "pressure_level": 1.90, "reason": "红K"},
        "ret_5d": 0.06,
        "current_price": 1.8,
        "market_cap": 10.0,
        "plan": {"entry_low": 1.75, "entry_high": 1.82, "target_1": 1.92, "target_2": 2.02, "stop_loss": 1.68, "rr": 2.0},
        "thesis": "HK AI app momentum",
    }
    us_swing = {
        "symbol": "AMD",
        "company_name": "AMD",
        "market": "US",
        "sector": "GPU",
        "pool": "ambush",
        "horizon": "swing",
        "rotation_score": 65.0,
        "priority_score": 65.0,
        "three_locks": {"status": "invalid", "score": 12.0, "support_level": 150.0, "pressure_level": 172.0, "reason": "蓝K"},
        "ret_5d": 0.04,
        "current_price": 160.0,
        "market_cap": 250.0,
        "plan": {"entry_tranches": [158.0, 150.0, 142.0], "target_1": 178.0, "target_2": 190.0, "stop_loss": 148.0},
        "thesis": "US swing",
    }
    cn_swing = {
        "symbol": "300214.SZ",
        "company_name": "日科化学",
        "market": "CN",
        "sector": "数据中心",
        "pool": "ambush",
        "horizon": "swing",
        "rotation_score": 62.0,
        "priority_score": 62.0,
        "three_locks": {"status": "invalid", "score": 10.0, "support_level": 9.2, "pressure_level": 11.2, "reason": "跌破支撑"},
        "ret_5d": 0.03,
        "current_price": 10.0,
        "market_cap": 5.0,
        "plan": {"entry_tranches": [9.8, 9.2, 8.6], "target_1": 12.5, "target_2": 13.8, "stop_loss": 8.9},
        "thesis": "CN swing",
    }
    return {
        "US": {
            "leading_sectors_today": [{"sector": "GPU"}],
            "fading_sectors_today": [],
            "cross_market_signals": [{"narrative": "US leads AI"}],
            "transmission_events": [],
            "sector_decision": {
                "session": "rotation",
                "market_scope": "US",
                "leading_sectors": [{"sector": "GPU", "market_scope": "US", "score": 1.0, "confidence": 0.5}],
                "active_sector_ids": ["GPU"],
                "winner_count": 1,
                "active_winner": "GPU",
                "rotation_regime": "focused",
                "allow_short_term_push": True,
                "contract_version": "2026-05-19-v1",
            },
            "candidate_set": [us_short, us_swing],
            "recommendations": [us_short, us_swing],
        },
        "AH": {
            "leading_sectors_today": [{"sector": "AI芯片"}, {"sector": "AI应用"}],
            "fading_sectors_today": [],
            "cross_market_signals": [{"narrative": "AH follows US"}],
            "transmission_events": [],
            "sector_decision": {
                "session": "rotation",
                "market_scope": "AH",
                "leading_sectors": [
                    {"sector": "AI芯片", "market_scope": "AH", "score": 1.0, "confidence": 0.5},
                    {"sector": "AI应用", "market_scope": "AH", "score": 0.8, "confidence": 0.5},
                ],
                "active_sector_ids": ["AI芯片", "AI应用"],
                "winner_count": 2,
                "active_winner": "AI芯片",
                "rotation_regime": "focused",
                "allow_short_term_push": True,
                "contract_version": "2026-05-19-v1",
            },
            "candidate_set": [cn_short, hk_short, cn_swing],
            "recommendations": [cn_short, hk_short, cn_swing],
        },
    }


def _fake_load_rotation(date_str: str, market: str) -> dict:
    del date_str
    return _fake_rotation_payloads()[market]


def _build_text_with_fixtures(date_str: str, session: str = "morning") -> str:
    with patch("send_discord_brief._load_rotation", side_effect=_fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        return build_brief_text(date_str, session)


def _build_payload_with_fixtures(date_str: str, session: str = "morning") -> dict:
    with patch("send_discord_brief._load_rotation", side_effect=_fake_load_rotation):
        return build_brief_payload(date_str, session)


def test_brief_contains_required_sections():
    text = _build_text_with_fixtures("2026-05-05")
    assert "盘前早报" in text
    assert "今日领涨赛道" in text
    assert "跨市场信号" in text


def test_brief_has_date_header():
    text = _build_text_with_fixtures("2026-05-05")
    assert "2026-05-05" in text


# ─── staleness warning ────────────────────────────────────────────────────

def test_staleness_note_returns_empty_when_fresh():
    """With fresh data (within 2 days), no warning is emitted."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        data_dir = root / "data"
        data_dir.mkdir()
        (data_dir / "candidates.json").write_text(json.dumps({"date": brief_module._today_cst(), "candidates": []}))
        with patch("send_discord_brief.PROJECT_ROOT", root):
            assert _data_staleness_note() == ""


def test_staleness_note_warns_on_stale_data():
    """Stale data (>2 days old) must trigger a ⚠️ warning."""
    from datetime import date, timedelta
    stale_date = str(date.today() - timedelta(days=5))
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        data_dir = root / "data"
        data_dir.mkdir()
        (data_dir / "candidates.json").write_text(json.dumps({"date": stale_date, "candidates": []}))
        with patch("send_discord_brief.PROJECT_ROOT", root):
            note = _data_staleness_note()
    assert "⚠️" in note


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


def test_pick_with_diversity_prefers_final_execution_score():
    candidates = [
        {"symbol": "EARLY", "market": "US", "execution_score": 10, "shortline_priority_score": 99},
        {"symbol": "FINAL", "market": "US", "execution_score": 80, "shortline_priority_score": 20},
    ]

    result = _pick_with_diversity(candidates, 1)

    assert result[0]["symbol"] == "FINAL"


# ─── session-aware scoring ────────────────────────────────────────────────

def test_session_score_excludes_extremely_overbought():
    """ret_5d above the hard cap must produce a strongly negative score regardless of base."""
    import sys
    sys.path.insert(0, "scripts")
    from send_discord_brief import _session_score

    rec = {"symbol": "XXX", "market": "US", "sector": "ai", "pool": "day_active",
           "ret_5d": 0.46, "rotation_score": 200.0}
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
    m = _build_text_with_fixtures("2026-05-05", "morning")
    d = _build_text_with_fixtures("2026-05-05", "midday")
    e = _build_text_with_fixtures("2026-05-05", "evening")

    assert "盘前早报" in m
    assert "A股午盘信号" in d
    assert "AI赛道短线精灵" in e
    assert "美股专区" in e


def test_midday_excludes_us_stocks():
    """Midday focus_markets={'CN','HK'} — US stocks must not appear in short_block."""
    payload = _build_payload_with_fixtures("2026-05-05", "midday")
    short_markets = {r["market"] for r in payload["short_block"]}
    assert "US" not in short_markets, f"Midday short block contained US stocks: {short_markets}"


def test_evening_excludes_ah_stocks():
    """Evening focus_markets={'US'} — CN and HK stocks must not appear in short_block."""
    payload = _build_payload_with_fixtures("2026-05-05", "evening")
    short_markets = {r["market"] for r in payload["short_block"]}
    assert "CN" not in short_markets and "HK" not in short_markets, (
        f"Evening short block contained AH stocks: {short_markets}"
    )


def test_evening_watchlist_can_surface_catalyst_limited_us_names():
    """20:30 is a prep watchlist: catalyst-limited names stay visible but marked as watch_only."""
    with patch("send_discord_brief._load_rotation", side_effect=_fake_load_rotation), \
         patch("send_discord_brief._earnings_payload_status", return_value=({"NVDA": {"symbol": "NVDA", "data_limited": True}}, "fresh")):
        payload = build_brief_payload("2026-05-05", "evening")

    nvda = next(item for item in payload["short_block"] if item["symbol"] == "NVDA")
    assert nvda["push_decision"] == "watch_only"
    assert "catalyst_data_limited" in nvda["reason_codes"]


def test_evening_watchlist_fills_from_candidate_set_to_five():
    payloads = _fake_rotation_payloads()
    extra_candidates = []
    for idx in range(1, 6):
        extra_candidates.append(
            {
                "symbol": f"USAI{idx}",
                "company_name": f"US AI {idx}",
                "market": "US",
                "sector": "GPU",
                "pool": "day_active",
                "rotation_score": 80.0 - idx,
                "priority_score": 80.0 - idx,
                "ret_5d": 0.05,
                "current_price": 20.0 + idx,
                "market_cap": 10.0,
                "atr_pct": 0.03,
                "active_sector": True,
                "rank_in_sector": idx + 1,
                "thesis": "US AI watchlist fill",
            }
        )
    payloads["US"]["candidate_set"] = payloads["US"]["candidate_set"] + extra_candidates
    payloads["US"]["recommendations"] = [payloads["US"]["recommendations"][0]]

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._earnings_payload_status", return_value=({}, "absent")):
        payload = build_brief_payload("2026-05-05", "evening")

    assert len(payload["short_block"]) == 5
    assert any(item["symbol"].startswith("USAI") for item in payload["short_block"])


def test_evening_watchlist_text_uses_layers_not_entry_command():
    text = _build_text_with_fixtures("2026-05-05", "evening")
    assert "美股专区" in text
    assert "结构：" in text
    assert "买入 " not in text


def test_evening_omits_bottleneck_from_payload_and_text():
    with patch("send_discord_brief._load_rotation", side_effect=_fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        payload = build_brief_payload("2026-05-05", "evening")
        text = build_brief_text("2026-05-05", "evening", payload=payload)

    assert payload["bottleneck_block"] == []
    assert all(item.get("horizon") != "bottleneck" for item in payload["watch_only"])
    assert "上游瓶颈侦察" not in text


def test_evening_renders_decision_sections_and_preserves_watchlist():
    text = _build_text_with_fixtures("2026-05-05", "evening")
    assert "30秒决策版" in text
    assert "美股专区" in text
    assert "港股专区" in text
    assert "A股专区" in text
    assert "禁区池｜看起来强，但盈亏比差" in text
    assert "关键映射链" in text
    assert "开盘脚本" in text
    assert "三把锁：" in text


def test_payload_contains_decision_layers():
    payload = _build_payload_with_fixtures("2026-05-05", "evening")
    assert payload["market_state"]["regime"]
    assert payload["opportunity_buckets"]["daytrade_focus"]
    assert "danger_pool" in payload
    assert "mapping_chain" in payload
    assert "open_script" in payload
    assert payload["market_sections"]["US"]["label"] == "美股专区"
    assert payload["market_sections"]["HK"]["label"] == "港股专区"
    assert payload["market_sections"]["CN"]["label"] == "A股专区"


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


def test_missing_market_goes_to_coverage_watch_not_short_block():
    payloads = _fake_rotation_payloads()
    payloads["US"]["recommendations"] = [payloads["US"]["recommendations"][0]]
    payloads["AH"]["recommendations"] = [payloads["AH"]["recommendations"][1], payloads["AH"]["recommendations"][2]]

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation):
        payload = build_brief_payload("2026-05-05", "morning")

    short_markets = {item["market"] for item in payload["short_block"]}
    assert "CN" not in short_markets
    coverage_markets = {item["market"] for item in payload["coverage_watch"]}
    swing_markets = {item["market"] for item in payload["swing_block"]}
    assert "CN" in coverage_markets or "CN" in swing_markets


def test_brief_renders_market_coverage_watchlist():
    payloads = _fake_rotation_payloads()
    payloads["US"]["recommendations"] = [payloads["US"]["recommendations"][0]]
    payloads["AH"]["recommendations"] = [payloads["AH"]["recommendations"][1], payloads["AH"]["recommendations"][2]]

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        text = build_brief_text("2026-05-05", "morning")

    assert "盘前早报" in text
    assert "今日领涨赛道" in text


def test_morning_now_renders_short_and_swing_blocks():
    text = _build_text_with_fixtures("2026-05-05", "morning")
    assert "美股专区" in text
    assert "港股专区" in text
    assert "A股专区" in text


def test_market_sections_show_scope_reference_in_evening():
    text = _build_text_with_fixtures("2026-05-05", "evening")

    assert "A股专区" in text
    assert "688256.SH" in text
    assert "[参考]" in text


def test_market_sections_surface_high_atr_as_observation():
    payloads = _fake_rotation_payloads()
    payloads["US"]["candidate_set"][0]["atr_pct"] = 0.10
    payloads["US"]["recommendations"][0]["atr_pct"] = 0.10

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        text = build_brief_text("2026-05-05", "morning")

    assert "NVDA" in text
    assert "[高波动观察]" in text


def test_data_limited_earnings_title_changes():
    payloads = _fake_rotation_payloads()

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    limited_play = {
        "symbol": "BIDU",
        "company_name": "Baidu, Inc.",
        "market": "US",
        "sector": "百度/文心",
        "earnings_date": "2026-05-19",
        "days_to_earnings": 0,
        "release_time": "pre-market",
        "eps_forecast": "$1.50",
        "eps_last_year": "$2.27",
        "fiscal_quarter": "Mar/2026",
        "data_limited": True,
    }

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[limited_play]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        text = build_brief_text("2026-05-19", "evening")

    assert "财报日历观察" not in text
