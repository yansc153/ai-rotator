from __future__ import annotations

from typing import Any


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def event_layer(item: dict[str, Any], earnings_index: dict[str, dict[str, Any]], earnings_state: str) -> dict[str, Any]:
    """Score near-term catalysts without doing network I/O in the sender."""
    score = 0.0
    tags: list[str] = []
    warnings: list[str] = []

    play = earnings_index.get(item.get("symbol", "")) if earnings_state == "fresh" else None
    if play:
        days = int(play.get("days_to_earnings", 99) or 99)
        if days <= 1:
            score += 8.0
            tags.append("earnings_0_1d")
        elif days <= 5:
            score += 4.0
            tags.append("earnings_week")
        else:
            tags.append("earnings_calendar")
        if play.get("data_limited"):
            score -= 3.0
            warnings.append("earnings_data_limited")

    if item.get("buyback_event") or item.get("buyback_authorized"):
        score += 5.0
        tags.append("buyback")
    if item.get("merger_event") or item.get("spinoff_event"):
        score += 6.0
        tags.append("corporate_action")
    if item.get("index_rebalance_event") or item.get("index_add_candidate"):
        score += 4.0
        tags.append("index_rebalance")

    if earnings_state in {"absent", "stale"} and item.get("market") == "US":
        warnings.append(f"event_source_{earnings_state}")

    return {"event_score": round(score, 4), "event_tags": tags, "event_warnings": warnings}


def insider_layer(item: dict[str, Any]) -> dict[str, Any]:
    cluster_score = _as_float(item.get("form4_cluster_score") or item.get("insider_cluster_score"), 0.0)
    net_selling = _as_float(item.get("insider_net_selling_pct") or item.get("insider_net_selling"), 0.0)
    tags: list[str] = []
    warnings: list[str] = []
    score = 0.0

    if cluster_score >= 0.7 and net_selling > 0:
        score -= 8.0
        warnings.append("form4_cluster_selling")
    elif cluster_score >= 0.5:
        score -= 3.0
        warnings.append("form4_activity")
    elif cluster_score <= -0.5:
        score += 4.0
        tags.append("insider_buying")

    return {"insider_score": round(score, 4), "insider_tags": tags, "insider_warnings": warnings}


def financial_layer(item: dict[str, Any]) -> dict[str, Any]:
    fcf_margin = _as_float(item.get("fcf_margin"), 0.0)
    fcf_yoy = _as_float(item.get("fcf_yoy"), 0.0)
    m_score = _as_float(item.get("beneish_m_score") or item.get("m_score"), -2.22)
    z_score = _as_float(item.get("altman_z_score") or item.get("z_score"), 3.0)
    score = 0.0
    warnings: list[str] = []
    tags: list[str] = []

    if fcf_margin < -0.05 or fcf_yoy < -0.35:
        score -= 6.0
        warnings.append("fcf_penalty")
    elif fcf_margin > 0.08:
        score += 3.0
        tags.append("fcf_quality")

    if m_score > -1.78:
        score -= 5.0
        warnings.append("m_score_watch")
    if z_score < 1.8:
        score -= 6.0
        warnings.append("z_score_distress")
    elif z_score > 3.0:
        score += 2.0
        tags.append("balance_sheet_ok")

    if item.get("capitalized_r_and_d_warning") or item.get("accounting_warning"):
        score -= 4.0
        warnings.append("accounting_quality")

    return {"financial_score": round(score, 4), "financial_tags": tags, "financial_warnings": warnings}


def risk_layer(item: dict[str, Any]) -> dict[str, Any]:
    atr_pct = _as_float(item.get("atr_pct"), 0.0)
    ret_5d = _as_float(item.get("ret_5d"), 0.0)
    drawdown = abs(_as_float(item.get("max_drawdown_20d") or item.get("drawdown_20d"), 0.0))
    hit_rate = item.get("hit_rate_20d") if item.get("hit_rate_20d") is not None else item.get("historical_hit_rate")
    score = 0.0
    warnings: list[str] = []
    tags: list[str] = []

    if atr_pct >= 0.09:
        score -= 8.0
        warnings.append("high_atr")
    elif atr_pct >= 0.06:
        score -= 3.0
        warnings.append("elevated_atr")
    elif 0.015 <= atr_pct <= 0.045:
        score += 2.0
        tags.append("controlled_vol")

    if ret_5d >= 0.30:
        score -= 6.0
        warnings.append("extended_5d")
    if drawdown >= 0.18:
        score -= 4.0
        warnings.append("drawdown_risk")
    if hit_rate is not None:
        hr = _as_float(hit_rate, 0.0)
        if hr >= 0.58:
            score += 4.0
            tags.append("hit_rate_ok")
        elif hr <= 0.42:
            score -= 4.0
            warnings.append("low_hit_rate")

    return {"risk_score": round(score, 4), "risk_tags": tags, "risk_warnings": warnings}


def apply_shortline_enrichment(
    item: dict[str, Any],
    *,
    session: str,
    earnings_index: dict[str, dict[str, Any]],
    earnings_state: str,
) -> dict[str, Any]:
    base_score = _as_float(item.get("_session_score") or item.get("rotation_score") or item.get("priority_score"), 0.0)
    event = event_layer(item, earnings_index, earnings_state)
    insider = insider_layer(item)
    financial = financial_layer(item)
    risk = risk_layer(item)
    macro_score = _as_float(item.get("macro_overlay_score"), 0.0)

    priority = base_score + event["event_score"] + insider["insider_score"] + financial["financial_score"] + risk["risk_score"] + macro_score
    if session == "evening" and item.get("market") == "US" and item.get("horizon", "short") == "short":
        priority += 3.0

    enriched = {
        **item,
        **event,
        **insider,
        **financial,
        **risk,
        "macro_overlay_score": round(macro_score, 4),
        "shortline_priority_score": round(priority, 4),
    }
    warnings = (
        event["event_warnings"]
        + insider["insider_warnings"]
        + financial["financial_warnings"]
        + risk["risk_warnings"]
    )
    enriched["warning_layer"] = warnings
    return enriched
