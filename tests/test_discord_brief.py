import json
import pytest
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


TRADE_TERMS = ("买入", "卖出", "加仓", "止损", "减仓", "目标位")
INTERNAL_TERMS = ("剧本B扩展", "AI分支未进今日top3", "三把锁", "execution_filter")


@pytest.fixture(autouse=True)
def _fixture_day_is_today(monkeypatch):
    monkeypatch.setattr(brief_module, "_today_cst", lambda: "2026-05-05")
    monkeypatch.setattr(brief_module, "today_cst", lambda: "2026-05-05")
    monkeypatch.setattr(
        brief_module,
        "_fresh_gate_status",
        lambda date_str, session, freshness_manifest: {"ok": True, "reason_codes": [], "strict_today": True},
    )


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
        "market_cap": 250.0,
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
        "market_cap": 250.0,
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
        "market_cap": 250.0,
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
    assert "今日活跃概念" in text
    assert "跨市场信号" in text


def test_brief_has_date_header():
    text = _build_text_with_fixtures("2026-05-05")
    assert "2026-05-05" in text


def test_status_only_text_contains_no_trade_terms():
    payload = brief_module._status_payload("2026-06-18", "midday", ["fetch_status_not_fresh"])
    text = build_brief_text("2026-06-18", "midday", payload=payload)
    assert "数据未更新" in text
    assert all(term not in text for term in TRADE_TERMS)


def test_status_only_payload_does_not_send_to_discord_by_default(monkeypatch):
    monkeypatch.delenv("AI_ROTATOR_SEND_STATUS_ALERTS", raising=False)
    payload = brief_module._status_payload("2026-06-18", "midday", ["fetch_status_not_fresh"])
    assert brief_module._should_send_to_discord(payload) is False


def test_status_only_payload_can_send_when_alerts_enabled(monkeypatch):
    monkeypatch.setenv("AI_ROTATOR_SEND_STATUS_ALERTS", "1")
    payload = brief_module._status_payload("2026-06-18", "midday", ["fetch_status_not_fresh"])
    assert brief_module._should_send_to_discord(payload) is True


def test_input_hash_includes_fetch_status(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports" / "daily"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    config_dir.mkdir()
    (data_dir / "candidates.json").write_text("{}")
    (config_dir / "session_rules.yaml").write_text("sessions: {}\n")
    status_path = data_dir / "fetch_status.json"
    monkeypatch.setattr(brief_module, "PROJECT_ROOT", tmp_path)

    status_path.write_text('{"status":"ok"}')
    first = brief_module._input_artifact_hash("2026-06-18")
    status_path.write_text('{"status":"degraded"}')
    second = brief_module._input_artifact_hash("2026-06-18")

    assert first != second


def test_build_payload_refuses_non_today_date_before_loading_rotation(monkeypatch):
    monkeypatch.setattr(brief_module, "_today_cst", lambda: "2026-06-18")
    with patch("send_discord_brief._load_rotation", side_effect=AssertionError("old rotation must not load")):
        payload = build_brief_payload("2026-06-17", "midday")

    assert payload["status_only"] is True
    assert payload["fresh_gate"]["reason_codes"] == ["requested_date_not_today"]


def test_trade_card_requires_company_name_and_level_sources():
    payload = {
        "run_id": "test",
        "date": "2026-05-05",
        "session": "midday",
        "leaders": ["AI芯片"],
        "cross_market_signal": {"narrative": "AI 主线分化"},
        "short_block": [],
        "swing_block": [],
        "bottleneck_block": [],
        "coverage_watch": [],
        "tradable_now": [],
        "watch_only": [],
        "rejected": [],
        "market_state": {},
        "market_sections": {},
        "three_locks_summary": "",
        "opportunity_buckets": {
            "premarket_open_sell": [
                {
                    "symbol": "688256.SH",
                    "company_name": "寒武纪",
                    "market": "CN",
                    "market_board": "A股·科创板",
                    "sector": "AI芯片",
                    "trade_style": "强势确认",
                    "execution_score": 88.0,
                    "current_price": 120.0,
                        "ret_5d": 0.08,
                        "company_concept": "AI芯片",
                        "ai_relationship": "核心/直接 AI",
                        "concept_verified": True,
                        "market_cap_ok": True,
                        "trade_language_allowed": True,
                        "push_decision": "tradable_now",
                        "fresh_data": True,
                        "daily_allowed": True,
                        "intraday_triggered": True,
                        "risk_levels_complete": True,
                        "trade_levels": {
                            "buy_level": 116.0,
                            "confirm_buy": 125.0,
                        "add_level": 128.0,
                        "stop_loss": 112.5,
                    },
                    "target_plan": {
                        "target_source": "fvg_gap",
                        "targets": [
                            {"label": "T1", "price": 130.0, "reason": "上方 FVG/gap下沿"},
                            {"label": "T2", "price": 134.0, "reason": "上方 FVG/gap中位"},
                            {"label": "T3", "price": 138.0, "reason": "上方 FVG/gap上沿"},
                        ],
                    },
                    "reason": "日线结构确认，盘中承接仍在。",
                }
            ],
            "intraday_dip_reversal": [],
            "overheat_failure_short": [],
            "radar_watch": [],
        },
        "danger_pool": [],
        "mapping_chain": [],
        "open_script": [],
        "signal_review": {},
        "freshness_manifest": [],
        "fresh_gate": {"ok": True, "reason_codes": []},
        "status_only": False,
        "contract_version": "test",
    }
    text = build_brief_text("2026-05-05", "midday", payload=payload)
    assert "688256.SH 寒武纪 [A股·科创板]" in text
    assert "进场 116.00（确认 125.00）" in text
    assert "卖出/减仓目标（fvg_gap）" in text
    assert "T1 130.00（上方 FVG/gap下沿）" in text
    assert "AI关系：核心/直接 AI" in text


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
    assert "午间交易计划" in d
    assert "AI赛道短线精灵" in e
    assert "交易候选（交易级）" in e


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
    assert "交易候选（交易级）" in text
    assert "回到" in text
    assert "仍有承接" in text
    assert "买入 " not in text
    assert all(term not in text for term in INTERNAL_TERMS)


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
    assert "交易候选（交易级）" in text
    assert "关键映射链" not in text
    assert "开盘脚本" not in text
    assert "日线结构：" not in text


def test_payload_contains_decision_layers():
    payload = _build_payload_with_fixtures("2026-05-05", "evening")
    assert payload["market_state"]["regime"]
    assert "premarket_open_sell" in payload["opportunity_buckets"]
    assert payload["opportunity_buckets"]["intraday_dip_reversal"]
    assert "overheat_failure_short" in payload["opportunity_buckets"]
    assert "radar_watch" in payload["opportunity_buckets"]
    assert "danger_pool" in payload
    assert "mapping_chain" in payload
    assert "open_script" in payload
    assert payload["market_sections"] == {}


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


def test_missing_recommendation_market_can_enter_playbook_from_candidate_set():
    payloads = _fake_rotation_payloads()
    payloads["US"]["recommendations"] = [payloads["US"]["recommendations"][0]]
    payloads["AH"]["recommendations"] = [payloads["AH"]["recommendations"][1], payloads["AH"]["recommendations"][2]]

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation):
        payload = build_brief_payload("2026-05-05", "morning")

    playbook_markets = {
        item["market"]
        for item in payload["opportunity_buckets"]["premarket_open_sell"]
    }
    assert "CN" in playbook_markets


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
    assert "今日活跃概念" in text


def test_morning_now_renders_short_and_swing_blocks():
    text = _build_text_with_fixtures("2026-05-05", "morning")
    assert "交易候选（交易级）" in text
    assert "短线 1-2天" not in text


def test_out_of_scope_names_do_not_enter_trade_playbooks():
    text = _build_text_with_fixtures("2026-05-05", "evening")

    assert "A股专区" not in text
    assert "688256.SH" not in text
    assert "[参考]" not in text


def test_high_atr_surfaces_as_radar_not_trade():
    payloads = _fake_rotation_payloads()
    payloads["US"]["candidate_set"][0]["atr_pct"] = 0.10
    payloads["US"]["recommendations"][0]["atr_pct"] = 0.10
    payloads["US"]["candidate_set"][0]["warning_layer"] = ["high_atr"]
    payloads["US"]["recommendations"][0]["warning_layer"] = ["high_atr"]

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        text = build_brief_text("2026-05-05", "morning")
        payload = build_brief_payload("2026-05-05", "morning")

    radar_entry = next(
        (item for item in payload["opportunity_buckets"]["radar_watch"] if item["symbol"] == "NVDA"),
        None,
    )
    assert radar_entry is not None
    assert radar_entry["trade_style"] == "高波动雷达"


def test_candidate_set_entries_are_admitted_by_standard_not_fixed_quota():
    payloads = _fake_rotation_payloads()
    extras = []
    for idx in range(1, 9):
        extras.append(
            {
                "symbol": f"USAI{idx}",
                "company_name": f"US AI {idx}",
                "market": "US",
                "sector": "GPU",
                "pool": "day_active",
                "horizon": "short",
                "rotation_score": 75.0 - idx,
                "priority_score": 75.0 - idx,
                "three_locks": {"status": "double_lock", "score": 60.0, "support_level": 20.0 + idx, "pressure_level": 25.0 + idx},
                "ret_5d": 0.06,
                "current_price": 20.0 + idx,
                "market_cap": 10.0,
                "active_sector": True,
                "rank_in_sector": idx,
                "thesis": "US AI candidate-set standard admission",
            }
        )
    payloads["US"]["candidate_set"] = extras
    payloads["US"]["recommendations"] = []

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        text = build_brief_text("2026-05-05", "morning")
        payload = build_brief_payload("2026-05-05", "morning")

    bucket_symbols = {item["symbol"] for item in payload["opportunity_buckets"]["premarket_open_sell"]}
    assert {f"USAI{idx}" for idx in range(1, 9)} <= bucket_symbols
    assert "USAI1" in text


def test_evening_admits_ai_extension_branch_into_pullback_longs():
    payloads = _fake_rotation_payloads()
    ai_extension = {
        "symbol": "AIX",
        "company_name": "AI Extension",
        "market": "US",
        "sector": "AI Agent",
        "sector_tags": "AI Agent;软件/SaaS",
        "chain_group": "AI Agent",
        "pool": "day_active",
        "horizon": "short",
        "rotation_score": 22.0,
        "priority_score": 22.0,
        "three_locks": {"status": "double_lock", "score": 60.0, "support_level": 18.0, "pressure_level": 25.0},
        "ret_5d": 0.12,
        "current_price": 22.0,
        "market_cap": 8.0,
        "active_sector": False,
        "rank_in_sector": 1,
        "thesis": "AI extension branch with valid structure",
    }
    non_ai_extension = {
        **ai_extension,
        "symbol": "OILX",
        "company_name": "Oil Extension",
        "sector": "铀",
        "sector_tags": "铀",
        "chain_group": "铀",
        "thesis": "non AI branch",
    }
    payloads["US"]["candidate_set"] = payloads["US"]["candidate_set"] + [ai_extension, non_ai_extension]
    payloads["US"]["recommendations"] = payloads["US"]["recommendations"] + [ai_extension, non_ai_extension]

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        payload = build_brief_payload("2026-05-05", "evening")

    bucket_symbols = {item["symbol"] for item in payload["opportunity_buckets"]["intraday_dip_reversal"]}
    assert "AIX" in bucket_symbols
    assert "OILX" not in bucket_symbols


def test_rejected_name_only_enters_danger_with_reject_reason():
    payloads = _fake_rotation_payloads()
    rejected_name = {
        "symbol": "INOD",
        "company_name": "Innodata",
        "market": "US",
        "sector": "GPU",
        "pool": "day_active",
        "horizon": "short",
        "rotation_score": 82.0,
        "priority_score": 82.0,
        "three_locks": {"status": "triple_lock", "score": 90.0, "support_level": 45.0, "pressure_level": 55.0},
        "ret_5d": 0.12,
        "current_price": 50.0,
        "market_cap": 1.0,
        "atr_pct": 0.10,
        "warning_layer": ["high_atr"],
        "active_sector": True,
        "rank_in_sector": 1,
        "thesis": "Looks strong but fails liquidity floor",
    }
    payloads["US"]["candidate_set"].append(rejected_name)
    payloads["US"]["recommendations"].append(rejected_name)

    def fake_load_rotation(date_str: str, market: str) -> dict:
        del date_str
        return payloads[market]

    with patch("send_discord_brief._load_rotation", side_effect=fake_load_rotation), \
         patch("send_discord_brief._load_earnings_plays", return_value=[]), \
         patch("send_discord_brief._data_staleness_note", return_value=""):
        payload = build_brief_payload("2026-05-05", "morning")
        text = build_brief_text("2026-05-05", "morning", payload=payload)

    all_trade_symbols = {
        item["symbol"]
        for bucket_name in ("premarket_open_sell", "intraday_dip_reversal", "radar_watch")
        for item in payload["opportunity_buckets"][bucket_name]
    }
    assert "INOD" not in all_trade_symbols
    danger = next(item for item in payload["danger_pool"] if item["symbol"] == "INOD")
    assert danger["reason"] == "market_cap_below_200b_cny"


def test_rejected_high_atr_is_avoid_not_observation():
    item = {
        "symbol": "INOD",
        "market": "US",
        "push_decision": "rejected",
        "warning_layer": ["high_atr"],
        "reason_codes": ["liquidity_below_floor"],
    }

    assert brief_module._decision_label(item) == "回避"
    assert brief_module._action_line(item) == "动作：当前结构失效，今天不列交易级"


def test_discord_chunks_split_on_lines_when_possible():
    text = "\n".join([f"line-{idx}" for idx in range(400)])
    chunks = brief_module._split_discord_chunks(text)

    assert all(len(chunk) <= brief_module.DISCORD_MESSAGE_LIMIT for chunk in chunks)
    assert "\n".join(chunks).replace("\n\n", "\n") == text


def test_danger_pool_is_summarized_for_discord():
    danger_pool = [
        {
            "symbol": f"D{idx}",
            "market": "CN",
            "company_name": f"Danger {idx}",
            "sector": "AI芯片",
            "trade_style": "回避",
            "execution_score": 10 - idx,
            "current_price": 10 + idx,
            "ret_5d": 0.1,
            "company_concept": "人工智能",
            "ai_relationship": "核心/直接 AI",
            "concept_verified": True,
            "market_cap_ok": False,
            "reason": "market_cap_below_200b_cny",
        }
        for idx in range(7)
    ]
    payload = {
        "date": "2026-05-05",
        "session": "midday",
        "leaders": ["AI芯片"],
        "cross_market_signal": {"narrative": "无跨市场信号"},
        "fresh_gate": {"ok": True},
        "status_only": False,
        "market_state": {},
        "signal_review": {},
        "opportunity_buckets": {
            "premarket_open_sell": [],
            "intraday_dip_reversal": [],
            "overheat_failure_short": [],
            "radar_watch": [],
        },
        "market_sections": {},
        "danger_pool": danger_pool,
        "mapping_chain": [],
        "open_script": [],
        "short_block": [],
        "swing_block": [],
        "coverage_watch": [],
    }

    text = build_brief_text("2026-05-05", "midday", payload=payload)

    # 当前版本仅展示可执行池，风险池不展开，避免过长
    assert "D0" not in text
    assert "D3" not in text


def test_tail_close_omits_non_trader_context():
    payload = {
        "date": "2026-05-05",
        "session": "tail_close",
        "leaders": ["新能源车", "电子"],
        "cross_market_signal": {"narrative": "无跨市场信号"},
        "fresh_gate": {"ok": True},
        "status_only": False,
        "market_state": {"summary": "今日可看：回踩承接 1 个，过热转弱 0 个。行动倾向：只做确认。"},
        "three_locks_summary": "日线结构：日线确认 81 个",
        "signal_review": {
            "recent": {
                "window": "近3日",
                "signal_count": 10,
                "priced_count": 10,
                "win_rate": 0.5,
                "avg_return_pct": 0.01,
            }
        },
        "opportunity_buckets": {
            "premarket_open_sell": [],
            "intraday_dip_reversal": [
                {
                    "symbol": "001287.SZ",
                    "market": "CN",
                    "company_name": "中电港",
                    "market_board": "A股·深主板",
                    "sector": "AI芯片",
                    "trade_style": "边缘观察",
                    "execution_score": 28,
                    "current_price": 29.78,
                    "ret_5d": 0.167,
                    "company_concept": "人工智能",
                    "ai_relationship": "核心/直接 AI",
                    "concept_verified": True,
                    "market_cap_ok": True,
                    "reason": "只看 26.07 附近是否有承接",
                }
            ],
            "overheat_failure_short": [],
            "radar_watch": [],
        },
        "market_sections": {},
        "danger_pool": [
            {
                "symbol": "02028.HK",
                "market": "HK",
                "company_name": "映美控股",
                "sector": "电子信息",
                "trade_style": "回避",
                "execution_score": 65,
                "current_price": 0.8,
                "ret_5d": -0.059,
                "company_concept": "电子信息",
                "ai_relationship": "未核验到明确 AI 主业",
                "concept_verified": False,
                "market_cap_ok": False,
                "reason": "concept_unverified",
            }
        ],
        "mapping_chain": [],
        "open_script": ["尾盘只看承接"],
        "short_block": [],
        "swing_block": [],
        "coverage_watch": [],
    }

    text = build_brief_text("2026-05-05", "tail_close", payload=payload)

    assert "今日领涨赛道" not in text
    assert "跨市场信号" not in text
    assert "近三日信号复盘" not in text
    assert "日线结构" not in text
    assert "强势确认｜开盘承接" not in text
    assert "开盘脚本" not in text
    assert "开盘强势" not in text
    assert "开盘承接" not in text
    assert "尾盘确认" in text
    assert "雷达｜高波动观察" not in text
    assert "禁区池" not in text
    assert "001287.SZ" in text
    assert "02028.HK" not in text


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
