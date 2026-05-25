"""
Tests for sector_rotation_agent — focused on fixes from May 2026 audit:
  - _parse_llm_json bare JSONDecodeError (was uncaught)
  - LLM narrative graceful degradation (local Codex CLI unavailable / failure)
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from tradingagents.agents.rotation.sector_rotation_agent import (
    _parse_llm_json,
    _build_prompt,
    _generate_llm_narratives,
    _apply_narratives,
)


# ─── _parse_llm_json ───────────────────────────────────────────────────────

def test_parse_llm_json_plain_json():
    raw = '{"sector_narratives": {"gpu": "AI领涨"}, "cross_signal_narrative": "US领先", "stock_theses": {}}'
    result = _parse_llm_json(raw)
    assert result["sector_narratives"]["gpu"] == "AI领涨"


def test_parse_llm_json_code_fenced():
    raw = '```json\n{"key": "value"}\n```'
    result = _parse_llm_json(raw)
    assert result == {"key": "value"}


def test_parse_llm_json_plain_text_returns_empty():
    """Regression: bare plain-text LLM output was raising uncaught JSONDecodeError."""
    raw = "今日AI板块领涨，建议关注GPU赛道。"
    result = _parse_llm_json(raw)
    assert result == {}


def test_parse_llm_json_malformed_code_fence_returns_empty():
    raw = "```\nnot valid { json at all\n```"
    result = _parse_llm_json(raw)
    assert result == {}


def test_parse_llm_json_empty_string_returns_empty():
    result = _parse_llm_json("   ")
    assert result == {}


# ─── _generate_llm_narratives graceful degradation ─────────────────────────

def test_generate_llm_narratives_codex_unavailable():
    """If local Codex CLI yields no JSON, returns {} without raising."""
    with patch(
        "tradingagents.agents.rotation.sector_rotation_agent.generate_json_object",
        return_value={},
    ):
        result = _generate_llm_narratives([], [], [])
    assert result == {}


def test_generate_llm_narratives_codex_exception():
    """Codex exception returns {} without propagating."""
    with patch(
        "tradingagents.agents.rotation.sector_rotation_agent.generate_json_object",
        side_effect=RuntimeError("boom"),
    ):
        result = _generate_llm_narratives([], [], [])
    assert result == {}


def test_generate_llm_narratives_parsed_json():
    """Codex JSON should flow through unchanged."""
    payload = {"sector_narratives": {"gpu": "AI领涨"}, "cross_signal_narrative": "", "stock_theses": {}}
    with patch(
        "tradingagents.agents.rotation.sector_rotation_agent.generate_json_object",
        return_value=payload,
    ):
        result = _generate_llm_narratives([], [], [])
    assert result == payload


# ─── _apply_narratives ─────────────────────────────────────────────────────

def test_apply_narratives_enriches_leaders():
    leaders = [{"sector": "gpu", "market": "US", "score": 1.0, "narrative": "default"}]
    signals = [{"narrative": "old signal"}]
    candidates = [{"symbol": "NVDA", "pool": "day_active", "llm_thesis": ""}]

    narratives = {
        "sector_narratives": {"gpu": "GPU主导AI基建"},
        "cross_signal_narrative": "美股领先港股3日",
        "stock_theses": {"NVDA": "算力龙头，受益AI资本开支"},
    }

    new_leaders, new_signals, new_cands = _apply_narratives(leaders, signals, candidates, narratives)
    assert new_leaders[0]["narrative"] == "GPU主导AI基建"
    assert new_signals[0]["narrative"] == "美股领先港股3日"
    assert new_cands[0]["llm_thesis"] == "算力龙头，受益AI资本开支"


def test_apply_narratives_missing_key_keeps_original():
    leaders = [{"sector": "missing_sector", "market": "CN", "score": 1.0, "narrative": "original"}]
    signals: list = []
    candidates = [{"symbol": "UNKNOWN", "pool": "ambush", "llm_thesis": "existing"}]

    narratives = {"sector_narratives": {}, "cross_signal_narrative": "", "stock_theses": {}}
    new_leaders, _, new_cands = _apply_narratives(leaders, signals, candidates, narratives)
    assert new_leaders[0]["narrative"] == "original"
    assert new_cands[0]["llm_thesis"] == "existing"  # unchanged


# ─── _build_prompt ──────────────────────────────────────────────────────────

def test_build_prompt_includes_ambush_stocks():
    """Ambush stocks must appear in prompt so they get LLM theses."""
    leaders = [{"sector": "gpu_ai_accelerator", "market": "US", "score": 5.0}]
    signals = [{"narrative": "US leads HK by 3d"}]
    candidates = [
        {"symbol": "NVDA", "company_name": "NVIDIA", "market": "US", "sector": "gpu",
         "ret_5d": 0.05, "atr_pct": 0.06, "pool": "day_active",
         "priority_score": 90, "rotation_score": 90},
        {"symbol": "0020.HK", "company_name": "商汤", "market": "HK", "sector": "ai_app",
         "ret_5d": -0.03, "atr_pct": 0.04, "pool": "ambush",
         "priority_score": 60, "rotation_score": 60},
    ]
    prompt = _build_prompt(leaders, signals, candidates)
    assert "0020.HK" in prompt
    assert "【中线左侧】" in prompt
    assert "【短线】" in prompt
    assert '"0020.HK": "___"' in prompt  # pre-populated JSON key
