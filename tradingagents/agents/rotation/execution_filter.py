from __future__ import annotations

import json
from datetime import datetime, time as dtime, timezone, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tradingagents.agents.rotation.common import normalize_symbol_for_file
from tradingagents.agents.rotation.company_concept import (
    ashare_board,
    market_board_label,
    market_cap_gate,
    verify_company_concept,
)
from tradingagents.agents.rotation.price_engine import build_trade_level_plan
from tradingagents.contracts.decision_chain import ExecutionDecision, FreshnessRecord
from tradingagents.runtime.paths import PROJECT_ROOT, RAW_DATA_DIR


_CST = timezone(timedelta(hours=8))
_SESSION_INTRADAY_CUTOFF = {
    "midday": dtime(11, 0),
    "tail_close": dtime(14, 0),
}


def _is_fresh_for_session(ts: pd.Timestamp, session: str) -> bool:
    cutoff = _SESSION_INTRADAY_CUTOFF.get(session)
    if cutoff is None:
        return True
    return (ts.hour, ts.minute, ts.second) >= (cutoff.hour, cutoff.minute, cutoff.second)


def _today_cst() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


def load_session_rules() -> dict[str, Any]:
    import yaml

    path = PROJECT_ROOT / "config" / "session_rules.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def session_meta(session: str) -> dict[str, Any]:
    sessions = load_session_rules().get("sessions", {})
    meta = dict(sessions.get(session, sessions.get("morning", {})))
    focus = meta.get("focus_markets")
    if focus is not None:
        meta["focus_markets"] = set(focus)
    return meta


def build_freshness_record(market: str, symbol: str, session: str, trade_date: str) -> FreshnessRecord:
    normalized = normalize_symbol_for_file(market, symbol)
    suffix = "15m" if market in {"CN", "HK", "US"} else "1h"
    path = RAW_DATA_DIR / f"{market}_{normalized}_{suffix}.csv"
    source_path = str(path)
    if not path.exists():
        return FreshnessRecord(
            symbol=symbol,
            market=market,
            session=session,
            intraday_status="missing",
            source_path=source_path,
        )
    try:
        frame = pd.read_csv(path)
        if frame.empty or "datetime" not in frame.columns:
            return FreshnessRecord(
                symbol=symbol,
                market=market,
                session=session,
                intraday_status="failed",
                source_path=source_path,
            )
        today_bars = frame[frame["datetime"].astype(str).str.startswith(trade_date)]
        if today_bars.empty:
            return FreshnessRecord(
                symbol=symbol,
                market=market,
                session=session,
                intraday_status="stale",
                source_path=source_path,
            )
        latest_ts = pd.to_datetime(today_bars.iloc[-1]["datetime"], errors="coerce")
        if latest_ts is None or pd.isna(latest_ts):
            return FreshnessRecord(
                symbol=symbol,
                market=market,
                session=session,
                intraday_status="failed",
                source_path=source_path,
            )
        if not _is_fresh_for_session(latest_ts, session):
            return FreshnessRecord(
                symbol=symbol,
                market=market,
                session=session,
                intraday_status="stale",
                as_of=str(latest_ts),
                bars_today=len(today_bars),
                source_path=source_path,
            )
        return FreshnessRecord(
            symbol=symbol,
            market=market,
            session=session,
            intraday_status="fresh",
            as_of=str(latest_ts),
            bars_today=len(today_bars),
            source_path=source_path,
        )
    except Exception:
        return FreshnessRecord(
            symbol=symbol,
            market=market,
            session=session,
            intraday_status="failed",
            source_path=source_path,
        )


def _latest_intraday_close(market: str, symbol: str, trade_date: str) -> float | None:
    normalized = normalize_symbol_for_file(market, symbol)
    path = RAW_DATA_DIR / f"{market}_{normalized}_15m.csv"
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path)
        today_bars = frame[frame["datetime"].astype(str).str.startswith(trade_date)]
        if today_bars.empty or "close" not in today_bars.columns:
            return None
        value = float(today_bars.iloc[-1]["close"])
        return value if value > 0 else None
    except Exception:
        return None


def build_freshness_manifest(items: list[dict[str, Any]], session: str, trade_date: str) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    records: list[dict[str, Any]] = []
    for item in items:
        key = (item["market"], item["symbol"])
        if key in seen:
            continue
        seen.add(key)
        records.append(build_freshness_record(item["market"], item["symbol"], session, trade_date).model_dump())
    return records


def _earnings_payload_status(trade_date: str) -> tuple[dict[str, dict[str, Any]], str]:
    path = PROJECT_ROOT / "data" / "earnings_plays.json"
    if not path.exists():
        return {}, "absent"
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}, "stale"
    if payload.get("date") != trade_date:
        return {}, "stale"
    plays = payload.get("earnings_plays", [])
    index = {row["symbol"]: row for row in plays if isinstance(row, dict) and row.get("symbol")}
    return index, "fresh"


def _uses_us_catalyst_gate(session: str, item: dict[str, Any]) -> bool:
    return session in {"evening", "us_prep", "us_rth_confirm"} and item.get("market") == "US" and item.get("horizon") == "short"


def catalyst_status(item: dict[str, Any], session: str, trade_date: str, earnings_index: dict[str, dict[str, Any]], earnings_state: str) -> str:
    del trade_date
    if not _uses_us_catalyst_gate(session, item):
        return "not_applicable"
    if earnings_state != "fresh":
        return earnings_state  # absent or stale
    play = earnings_index.get(item["symbol"])
    if play is None:
        return "absent"
    if play.get("data_limited"):
        return "data_limited"
    return "fresh"


def classify_candidate(
    item: dict[str, Any],
    *,
    session: str,
    trade_date: str,
    active_sector_ids: list[str],
    earnings_index: dict[str, dict[str, Any]],
    earnings_state: str,
) -> dict[str, Any]:
    meta = session_meta(session)
    focus = meta.get("focus_markets")
    freshness = build_freshness_record(item["market"], item["symbol"], session, trade_date)
    c_status = catalyst_status(item, session, trade_date, earnings_index, earnings_state)
    horizon = item.get("horizon", "short")
    reason_codes: list[str] = []
    invalid_if: list[str] = []
    push_decision = "tradable_now"
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    three_locks_status = str(three_locks.get("status", "insufficient_history"))
    concept_keys = {
        "company_concept",
        "concept_verified",
        "concept_status",
        "concept_source",
        "concept_source_url",
        "concept_evidence_date",
        "concept_confidence",
        "ai_relationship",
        "ai_relevance",
    }
    if concept_keys <= set(item.keys()):
        concept = {key: item.get(key) for key in concept_keys}
    else:
        concept = verify_company_concept(item, evidence_date=trade_date)
    cap_gate = market_cap_gate(item.get("market", ""), item.get("market_cap"))
    daily_allowed = three_locks_status in {"double_lock", "triple_lock"} and not three_locks.get("breakdown_support")

    if focus is not None and item.get("market") not in focus:
        push_decision = "rejected"
        reason_codes.append("market_out_of_scope")

    if not cap_gate["market_cap_ok"]:
        push_decision = "rejected"
        reason_codes.append("market_cap_below_200b_cny")

    if concept["concept_status"] in {"pseudo_ai", "unverified"}:
        push_decision = "rejected"
        reason_codes.append(f"concept_{concept['concept_status']}")
    elif not concept["concept_verified"] and push_decision == "tradable_now":
        push_decision = "watch_only"
        reason_codes.append("concept_not_verified")

    active_sector = bool(item.get("active_sector"))
    if push_decision == "tradable_now" and horizon == "short" and meta.get("require_active_sector_for_short", True):
        if not active_sector or item.get("sector") not in active_sector_ids:
            push_decision = "watch_only"
            reason_codes.append("not_in_active_sector")

    intraday_required = bool(meta.get("require_fresh_intraday", False)) or float(meta.get("intraday_weight", 0.0) or 0.0) > 0
    if push_decision == "tradable_now" and horizon == "short" and intraday_required:
        if freshness.intraday_status != "fresh":
            push_decision = "watch_only"
            reason_codes.append(f"intraday_{freshness.intraday_status}")

    min_market_caps = meta.get("min_market_cap", {})
    min_cap = float(min_market_caps.get(item.get("market"), 0))
    market_cap = float(item.get("market_cap", 0.0) or 0.0)
    if push_decision != "rejected" and market_cap < min_cap:
        push_decision = "rejected"
        reason_codes.append("liquidity_below_floor")

    session_score = float(item.get("_session_score", item.get("rotation_score", 0.0)))
    if push_decision == "tradable_now" and session_score < 0:
        push_decision = "rejected"
        reason_codes.append("session_score_negative")

    if push_decision != "rejected" and _uses_us_catalyst_gate(session, item):
        if c_status == "data_limited":
            push_decision = "watch_only"
            reason_codes.append("catalyst_data_limited")
        elif c_status in {"absent", "stale"}:
            rank = int(item.get("rank_in_sector", 999))
            if not (session_score >= float(meta.get("structure_only_min_score", 40.0)) and rank <= int(meta.get("structure_only_max_rank", 3))):
                push_decision = "watch_only"
                reason_codes.append("no_catalyst_no_clean_structure")

    if horizon == "swing" and push_decision == "tradable_now":
        push_decision = "watch_only"
        reason_codes.append("swing_watch_only")

    warnings = set(item.get("warning_layer", []) or [])
    if push_decision == "tradable_now" and "high_atr" in warnings:
        push_decision = "watch_only"
        reason_codes.append("high_atr_watch_only")

    if push_decision == "tradable_now" and three_locks_status == "invalid":
        push_decision = "watch_only"
        reason_codes.append("three_locks_invalid")
    if push_decision == "tradable_now" and not daily_allowed:
        push_decision = "watch_only"
        reason_codes.append("daily_structure_not_confirmed")

    if active_sector:
        invalid_if.append("sector_leader_breaks")
    if intraday_required and freshness.intraday_status != "fresh":
        invalid_if.append(f"intraday_{freshness.intraday_status}")
    if c_status in {"stale", "absent", "data_limited"} and _uses_us_catalyst_gate(session, item):
        invalid_if.append(f"catalyst_{c_status}")
    if "high_atr" in warnings:
        invalid_if.append("high_atr")
    if three_locks_status == "invalid":
        invalid_if.append("three_locks_invalid")
    if three_locks.get("breakdown_support"):
        invalid_if.append("three_locks_support_break")

    price_source = "daily_structure"
    level_item = {**item, "three_locks": three_locks, "price_source": price_source}
    intraday_close = None
    if freshness.intraday_status == "fresh":
        intraday_close = _latest_intraday_close(item["market"], item["symbol"], trade_date)
        if intraday_close is not None:
            price_source = "intraday_15m"
            level_item["current_price"] = intraday_close
            level_item["price_source"] = price_source
    trade_levels = build_trade_level_plan(level_item)
    intraday_triggered = (freshness.intraday_status == "fresh" or not intraday_required) and horizon == "short"
    fresh_data = freshness.intraday_status == "fresh" or not intraday_required
    risk_levels_complete = bool(trade_levels.get("complete"))
    trade_language_allowed = all(
        [
            fresh_data,
            concept["concept_verified"],
            cap_gate["market_cap_ok"],
            daily_allowed,
            intraday_triggered,
            risk_levels_complete,
            push_decision == "tradable_now",
        ]
    )

    score = session_score
    if active_sector:
        score += 10.0
    if freshness.intraday_status == "fresh":
        score += 5.0
    if c_status == "fresh":
        score += 8.0
    if three_locks_status == "triple_lock":
        score += 12.0
    elif three_locks_status == "double_lock":
        score += 6.0
    elif three_locks_status == "invalid":
        score -= 10.0
    if push_decision == "watch_only":
        score -= 15.0
    if push_decision == "rejected":
        score -= 40.0

    payload = {
        **item,
        **concept,
        **cap_gate,
        "push_decision": push_decision,
        "execution_score": round(score, 4),
        "reason_codes": reason_codes,
        "invalid_if": invalid_if,
        "freshness_status": freshness.intraday_status,
        "freshness_record": freshness.model_dump(),
        "price_source": price_source,
        "intraday_current_price": intraday_close,
        "catalyst_status": c_status,
        "a_share_board": ashare_board(item.get("symbol", ""), item.get("market")),
        "market_board": market_board_label(item.get("symbol", ""), item.get("market", "")),
        "daily_allowed": daily_allowed,
        "intraday_triggered": intraday_triggered,
        "fresh_data": fresh_data,
        "risk_levels_complete": risk_levels_complete,
        "trade_language_allowed": trade_language_allowed,
        "trade_levels": trade_levels,
        "target_plan": trade_levels.get("target_plan", {}),
    }
    ExecutionDecision.model_validate(
        {
            "symbol": payload["symbol"],
            "market": payload["market"],
            "sector": payload["sector"],
            "horizon": payload.get("horizon", "short"),
            "push_decision": payload["push_decision"],
            "execution_score": float(payload["execution_score"]),
            "reason_codes": payload["reason_codes"],
            "invalid_if": payload["invalid_if"],
            "freshness_status": payload["freshness_status"],
            "catalyst_status": payload["catalyst_status"],
            "active_sector": bool(payload.get("active_sector", False)),
            "rank_in_sector": payload.get("rank_in_sector"),
            "sector_fit_score": payload.get("sector_fit_score"),
        }
    )
    return payload
