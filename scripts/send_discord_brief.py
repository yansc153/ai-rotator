from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, time as dtime, timezone, timedelta
from typing import Any
from uuid import uuid4
import certifi
import requests  # handles SSL EOF gracefully on Python 3.14+

import yaml
from _common import PROJECT_ROOT, load_env_file
from storage.sqlite import insert_decision_ledger
from tradingagents.agents.rotation.signal_review import (
    build_recent_review_summary,
    build_weekly_review_summary,
    record_signals_from_payload,
    refresh_signal_outcomes,
)
from tradingagents.agents.rotation.execution_filter import (
    build_freshness_manifest,
    classify_candidate,
    session_meta as execution_session_meta,
    _earnings_payload_status,
)
from tradingagents.agents.rotation.shortline_enrichment import apply_shortline_enrichment
from tradingagents.contracts.decision_chain import CONTRACT_VERSION, DecisionLedgerRow, PushPayload
from tradingagents.runtime import read_fetch_manifest, today_cst

_CST = timezone(timedelta(hours=8))


def _today_cst() -> str:
    """Return today's date string in CST (UTC+8), e.g. '2026-05-15'.

    GitHub Actions runs in UTC; calling date.today() at 01:00 UTC (= 09:00 CST)
    returns the UTC date which is one day behind from the user's perspective.
    Always use this helper instead of str(date.today()) throughout this module.
    """
    return datetime.now(_CST).strftime("%Y-%m-%d")

def _session_meta(session: str) -> dict[str, Any]:
    meta = execution_session_meta(session)
    if not meta:
        raise RuntimeError(f"session_rules missing or invalid for session={session}")
    if "send_to_discord" not in meta:
        raise RuntimeError(f"session_rules missing send_to_discord for session={session}")
    return meta

def _load_intraday_overlay(market: str, symbol: str, session: str = "morning") -> dict[str, float]:
    """Return {ret_intraday, overextended} from latest intraday CSV for today.

    ret_intraday = (last_bar_close - first_bar_close_today) / first_bar_close_today
    Returns zeros if the file is missing or today has no bars (pre-open).
    """
    import pandas as pd
    from tradingagents.agents.rotation.common import RAW_DIR, normalize_symbol_for_file

    normalized = normalize_symbol_for_file(market, symbol)
    suffix = "15m" if session in {"midday", "tail_close"} and market in {"CN", "HK"} else "1h"
    path = RAW_DIR / f"{market}_{normalized}_{suffix}.csv"
    if not path.exists():
        return {"ret_intraday": 0.0, "overextended": False}
    try:
        df = pd.read_csv(path)
        if df.empty or "datetime" not in df.columns:
            return {"ret_intraday": 0.0, "overextended": False}
        today_str = _today_cst()
        today_bars = df[df["datetime"].str.startswith(today_str)]
        if today_bars.empty:
            return {"ret_intraday": 0.0, "overextended": False}
        latest_ts = pd.to_datetime(today_bars.iloc[-1]["datetime"], errors="coerce")
        if latest_ts is None or pd.isna(latest_ts):
            return {"ret_intraday": 0.0, "overextended": False}
        overlay_cutoff = {"midday": dtime(11, 0), "tail_close": dtime(14, 0)}.get(session)
        if overlay_cutoff is not None and (latest_ts.hour, latest_ts.minute, latest_ts.second) < (
            overlay_cutoff.hour,
            overlay_cutoff.minute,
            overlay_cutoff.second,
        ):
            return {"ret_intraday": 0.0, "overextended": False}
        open_close = float(today_bars.iloc[0]["close"])
        last_close = float(today_bars.iloc[-1]["close"])
        if open_close <= 0:
            return {"ret_intraday": 0.0, "overextended": False}
        ret_id = (last_close - open_close) / open_close
        return {"ret_intraday": ret_id, "overextended": False}  # caller sets overextended
    except Exception:
        return {"ret_intraday": 0.0, "overextended": False}


def _session_score(item: dict[str, Any], session: str) -> float:
    """Return session-adjusted score for re-ranking candidates.

    Three adjustments:
    1. Overbought penalty   — ret_5d > threshold removes the stock from contention
    2. Intraday momentum    — healthy same-day move boosts score
    3. Overextension penalty — already ran too far today = disqualified
    """
    cfg = _session_meta(session)
    base = float(item.get("rotation_score") or item.get("priority_score") or 0)

    # 1. Overbought filter (daily)
    ret_5d = float(item.get("ret_5d", 0.0))
    hard_overbought = float(cfg.get("hard_overbought_5d", 0.45))
    soft_overbought = float(cfg["overbought_5d"])
    if ret_5d > hard_overbought:
        # Extremely overbought — hard exclude
        overbought_penalty = base + 200.0
    elif ret_5d > soft_overbought:
        # Session-tuned overbought penalty.
        excess = ret_5d - soft_overbought
        overbought_penalty = excess * 180
    else:
        overbought_penalty = 0.0

    # 2 & 3. Intraday overlay
    intraday = _load_intraday_overlay(item.get("market", "US"), item.get("symbol", ""), session)
    ret_id = intraday["ret_intraday"]
    intraday_weight = cfg["intraday_weight"]

    overextended_threshold = cfg["overextended_intraday"]
    if ret_id > overextended_threshold:
        # Stock already ran too far today — strong penalty
        intraday_bonus = -50.0
    elif ret_id > 0.01:
        # Healthy momentum: +5 to +20 pts depending on move magnitude and session weight
        intraday_bonus = ret_id * 500 * intraday_weight
    elif ret_id < -0.02:
        # Down >2% today — penalise for short session, slight bonus for ambush
        pool = item.get("pool", "")
        intraday_bonus = 10.0 * intraday_weight if pool == "ambush" else -15.0 * intraday_weight
    else:
        # Flat or barely moved — neutral
        intraday_bonus = ret_id * 200 * intraday_weight

    return base - overbought_penalty + intraday_bonus


def _load_sector_aliases() -> dict[str, str]:
    path = PROJECT_ROOT / "config" / "sector_aliases.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("aliases", {})

SECTOR_ALIASES = _load_sector_aliases()

def _sector_cn(code: str) -> str:
    return SECTOR_ALIASES.get(code, code)

DISCORD_API_HOST = "https://discord.com/api/v10"
DISCORD_MESSAGE_LIMIT = 2000
PLAYBOOK_RENDER_LIMIT = 3
DANGER_RENDER_LIMIT = 3
THREE_LOCK_LABELS = {
    "triple_lock": "日线强确认",
    "double_lock": "日线确认",
    "single_lock": "日线观察",
    "invalid": "结构失效",
    "insufficient_history": "数据不足",
}
RADAR_MIN_SCORE = 35.0
DANGER_MIN_SCORE = -15.0
AI_ROTATION_KEYWORDS = (
    "AI",
    "人工智能",
    "算力",
    "数据",
    "光通信",
    "AIOps",
    "DevOps",
    "Agent",
    "服务器",
    "芯片",
    "半导体",
    "存储",
    "watsonx",
    "GPU",
    "云计算",
)
def _status_payload(date_str: str, session: str, reason_codes: list[str]) -> dict[str, Any]:
    payload = {
        "run_id": str(uuid4()),
        "date": date_str,
        "session": session,
        "leaders": [],
        "cross_market_signal": {"narrative": "数据未更新，今日无交易级信号。"},
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
        "opportunity_buckets": {},
        "danger_pool": [],
        "mapping_chain": [],
        "open_script": [],
        "signal_review": {},
        "freshness_manifest": [],
        "fresh_gate": {"ok": False, "reason_codes": reason_codes},
        "status_only": True,
        "send_status": "status_only",
        "contract_version": CONTRACT_VERSION,
    }
    PushPayload.model_validate(payload)
    return payload


def _fresh_gate_status(date_str: str, session: str, freshness_manifest: list[dict[str, Any]]) -> dict[str, Any]:
    meta = _session_meta(session)
    reason_codes: list[str] = []
    if date_str != _today_cst():
        return {"ok": False, "reason_codes": ["requested_date_not_today"], "strict_today": True}

    manifest = read_fetch_manifest()
    if manifest.get("trade_date") != today_cst() or manifest.get("status") != "ok":
        reason_codes.append("fetch_status_not_fresh")

    candidates_path = PROJECT_ROOT / "data" / "candidates.json"
    try:
        candidates = json.loads(candidates_path.read_text())
        if candidates.get("date") != today_cst():
            reason_codes.append("candidates_not_fresh")
    except Exception:
        reason_codes.append("candidates_missing")

    if meta.get("require_fresh_intraday", False):
        records = freshness_manifest or []
        focus = meta.get("focus_markets")
        if focus is not None:
            records = [record for record in records if record.get("market") in focus]
        if not records or any(record.get("intraday_status") != "fresh" for record in records):
            reason_codes.append("intraday_not_fresh")

    return {"ok": not reason_codes, "reason_codes": reason_codes, "strict_today": True}


def _freshness_gate_items(classified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in classified if item.get("push_decision") != "rejected"]


def _status_text(date_str: str, session: str, payload: dict[str, Any]) -> str:
    meta = _session_meta(session)
    reasons = payload.get("fresh_gate", {}).get("reason_codes", []) or ["data_not_ready"]
    reason_map = {
        "rotation_missing": "轮动报告未生成",
        "requested_date_not_today": "请求日期不是今天",
        "fetch_status_not_fresh": "行情抓取状态不是今天",
        "candidates_not_fresh": "候选池不是今天生成",
        "candidates_missing": "候选池不存在",
        "intraday_not_fresh": "盘中数据未更新",
        "data_not_ready": "数据未准备好",
    }
    readable = "；".join(reason_map.get(str(code), str(code)) for code in reasons)
    return "\n".join(
        [
            f"{meta['label']} · {date_str}",
            "数据未更新，今日无交易级信号。",
            f"原因：{readable}",
            "处理：等待下一轮数据刷新后再发布正常观察卡。",
        ]
    )


def _load_rotation(date_str: str, market: str) -> dict[str, Any]:
    """Load a prebuilt daily rotation report for a given market."""
    path = PROJECT_ROOT / "reports" / "daily" / f"{date_str}-{market.lower()}-rotation.json"
    if not path.exists():
        raise FileNotFoundError(
            f"[rotation] Missing {market} rotation report for {date_str}: {path}. "
            "Run run_daily_rotation.py before send_discord_brief.py."
        )
    return json.loads(path.read_text())


def _candidate_pool_for_session(
    us: dict[str, Any],
    ah: dict[str, Any],
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    def add_unique(items: list[dict[str, Any]], seen: set[tuple[str, str]], output: list[dict[str, Any]]) -> None:
        for row in items:
            if not isinstance(row, dict):
                continue
            market = str(row.get("market", ""))
            symbol = str(row.get("symbol", ""))
            if not market or not symbol:
                continue
            key = (market, symbol)
            if key in seen:
                continue
            seen.add(key)
            output.append(dict(row))

    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    add_unique(us.get("recommendations", []) or [], seen, output)
    add_unique(ah.get("recommendations", []) or [], seen, output)
    if meta.get("include_candidate_set", True) or meta.get("short_block_source") == "watchlist":
        add_unique(us.get("candidate_set", []) or [], seen, output)
        add_unique(ah.get("candidate_set", []) or [], seen, output)
    return output


def _cap_tier(market_cap: float) -> str:
    """Classify market cap into tiers to enforce diversity in pick selection."""
    if market_cap <= 0:
        return "unknown"
    if market_cap >= 500:     # ≥$500B / ≥500亿HKD equivalent
        return "mega"
    if market_cap >= 50:      # $50-500B
        return "large"
    if market_cap >= 5:       # $5-50B
        return "mid"
    return "small"            # <$5B


def _pick_with_diversity(candidates: list[dict], n: int) -> list[dict]:
    """Pick n items ensuring market diversity AND cap-tier diversity.

    Rules:
    1. Reserve 1 slot per market present (CN / HK / US)
    2. Cap mega-cap (≥$500B) stocks at max 2 slots total — prevents NVDA/AMD/TSM
       from filling all 5 short slots when US data improves
    3. Fill remaining slots by session-aware score descending

    Uses the final execution_score after classification. Earlier layer scores
    are fallback only; they must not override the final push decision score.
    """
    def _score(item: dict) -> float:
        for key in ("execution_score", "shortline_priority_score", "_session_score"):
            if key in item:
                return float(item[key])
        return float(item.get("rotation_score") or item.get("priority_score") or 0)

    by_market: dict[str, list[dict]] = {}
    for item in candidates:
        by_market.setdefault(item["market"], []).append(item)

    result: list[dict] = []
    seen_symbols: set[str] = set()
    mega_cap_count = 0
    MEGA_CAP_MAX = 2  # at most 2 mega-cap stocks per block

    # Phase 1: guarantee 1 slot per market (pick highest-scored non-mega or mega if no choice)
    markets_present = list(by_market.keys())
    for market in markets_present:
        if len(result) >= n:
            break
        # Try non-mega first, then fall through to mega if no non-mega available
        pool = sorted(by_market[market], key=_score, reverse=True)
        for item in pool:
            if item["symbol"] in seen_symbols:
                continue
            tier = _cap_tier(float(item.get("market_cap", 0)))
            if tier == "mega" and mega_cap_count >= MEGA_CAP_MAX:
                continue
            result.append(item)
            seen_symbols.add(item["symbol"])
            if tier == "mega":
                mega_cap_count += 1
            break

    # Phase 2: fill remaining slots by score, respecting mega-cap cap
    remaining = sorted(candidates, key=_score, reverse=True)
    for item in remaining:
        if len(result) >= n:
            break
        if item["symbol"] in seen_symbols:
            continue
        tier = _cap_tier(float(item.get("market_cap", 0)))
        if tier == "mega" and mega_cap_count >= MEGA_CAP_MAX:
            continue
        result.append(item)
        seen_symbols.add(item["symbol"])
        if tier == "mega":
            mega_cap_count += 1

    # Phase 3: if still not enough, relax mega-cap constraint to fill remaining slots
    for item in remaining:
        if len(result) >= n:
            break
        if item["symbol"] not in seen_symbols:
            result.append(item)
            seen_symbols.add(item["symbol"])

    return result[:n]


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "暂无"
    try:
        return f"{float(value):+.1%}"
    except (TypeError, ValueError):
        return "暂无"


def _fmt_price(value: Any) -> str:
    if value is None:
        return "暂无"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "暂无"


def _three_locks_label(item: dict[str, Any]) -> str:
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    return THREE_LOCK_LABELS.get(str(three_locks.get("status", "insufficient_history")), "数据不足")


def _decision_label(item: dict[str, Any]) -> str:
    warnings = set(item.get("warning_layer", []) or [])
    reasons = set(item.get("reason_codes", []) or [])
    if item.get("push_decision") == "rejected":
        return "参考" if "market_out_of_scope" in reasons else "回避"
    if "high_atr" in warnings or "high_atr_watch_only" in reasons:
        return "高波动观察"
    if item.get("push_decision") == "tradable_now":
        return "优先"
    return "可观望" if item.get("market") == "CN" else "观察"


def _technical_line(item: dict[str, Any]) -> str:
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    reason = str(three_locks.get("reason") or "").strip()
    if not reason:
        return "结构：暂无日线确认"
    parts = [f"结构：{reason}"]
    support = three_locks.get("support_level")
    pressure = three_locks.get("pressure_level")
    if pressure is not None:
        parts.append(f"压力 {_fmt_price(pressure)}")
    if support is not None:
        parts.append(f"支撑 {_fmt_price(support)}")
    return " | ".join(parts)


def _action_line(item: dict[str, Any]) -> str:
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    label = _decision_label(item)
    support = three_locks.get("support_level")
    pressure = three_locks.get("pressure_level")
    if label == "优先":
        if support is not None:
            return f"动作：可盯回踩不破 {_fmt_price(support)}；跌破取消"
        return "动作：只做盘中确认后的突破/回踩，不自动追"
    if label == "高波动观察":
        return "动作：波动过高，等缩量回踩或新结构确认"
    if label == "回避":
        return "动作：当前结构失效，今天不列交易级"
    if pressure is not None:
        return f"动作：突破 {_fmt_price(pressure)} 再升级，否则只看"
    return "动作：先观察，不作为当前交易级"


def _bucket_trade_style(item: dict[str, Any]) -> str:
    warnings = set(item.get("warning_layer", []))
    risk_score = float(item.get("risk_score", 0.0) or 0.0)
    if item.get("push_decision") == "rejected":
        return "回避"
    if item.get("pool") == "ambush" or item.get("horizon") == "swing":
        return "低吸观察"
    if {"extended_5d", "high_atr"} & warnings or risk_score <= -6.0:
        return "等确认"
    if item.get("push_decision") == "tradable_now":
        return "追强"
    return "隔夜观察"


def _item_reason(item: dict[str, Any]) -> str:
    thesis = str(item.get("thesis") or item.get("why_buy") or "").strip()
    if thesis:
        return thesis
    warnings = item.get("warning_layer", [])
    if warnings:
        return "注意 " + " / ".join(str(x) for x in warnings[:2])
    sector = _sector_cn(item.get("sector", ""))
    return f"{sector} 方向继续观察"


def _fmt_targets(item: dict[str, Any]) -> str:
    target_plan = item.get("target_plan") if isinstance(item.get("target_plan"), dict) else {}
    targets = target_plan.get("targets") if isinstance(target_plan.get("targets"), list) else []
    if not targets:
        return "目标未生成"
    parts: list[str] = []
    for row in targets[:3]:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label", "T")).strip()
        price = _fmt_price(row.get("price") if row.get("price") is not None else row.get("target"))
        reason = str(row.get("reason", "")).strip()
        if reason:
            parts.append(f"{label} {price}（{reason}）")
        else:
            parts.append(f"{label} {price}")
    source = target_plan.get("target_source")
    if not source:
        source = target_plan.get("method", "unavailable")
    if parts:
        joined = "；".join(parts)
        return f"卖出/减仓：{joined}｜算法 {source}"
    return f"卖出/减仓：未生成｜算法 {source}"


def _holding_plan(session: str) -> str:
    if session == "tail_close":
        return "15m级别，建议持仓 0.5-1.5h（尾盘避免追高）"
    return "15m级别，建议持仓 0.5-2h"


def _resolve_a_stock_board(symbol: str, market_board: str | None, market: str) -> str:
    if market_board and str(market_board).strip():
        return str(market_board)
    if market != "CN":
        return {"HK": "港股", "US": "美股"}.get(market, "其他")

    code = str(symbol or "").split(".")[0].strip()
    if not code:
        return "A股·主板"

    if any(code.startswith(prefix) for prefix in ("300", "301", "30")):
        return "A股·创业板"
    if any(code.startswith(prefix) for prefix in ("68", "689")):
        return "A股·科创板"
    if code.startswith("60"):
        return "A股·沪主板"
    if code.startswith(("000", "001", "002", "003", "00")):
        return "A股·深主板"
    return "A股·深主板"


def _board_key(board: str) -> tuple[int, str]:
    if board.startswith("A股·"):
        if "科创板" in board:
            return 1, board
        if "创业板" in board:
            return 2, board
        if "沪主板" in board:
            return 3, board
        return 0, board
    if board == "美股":
        return 4, board
    if board == "港股":
        return 5, board
    return 5, board


def _session_sector_summary(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    buckets = payload.get("opportunity_buckets", {}) if isinstance(payload.get("opportunity_buckets"), dict) else {}
    bucket_items = []
    for key in ("premarket_open_sell", "intraday_dip_reversal"):
        bucket_items.extend(item for item in buckets.get(key, []) or [] if isinstance(item, dict))
    tradable = payload.get("tradable_now", []) or []
    weak_pool = payload.get("watch_only", []) or []
    watchlist = payload.get("short_block", []) or []
    all_items = tradable + weak_pool + watchlist + bucket_items
    if not all_items:
        return [], []

    sector_scores: dict[str, float] = {}
    for item in all_items:
        sector = str(item.get("sector", "") or "未归类")
        score = float(item.get("execution_score", item.get("rotation_score", 0.0) or 0.0) or 0.0)
        sector_scores.setdefault(sector, 0.0)
        sector_scores[sector] = max(sector_scores[sector], score)

    tradable_sectors = {
        str(item.get("sector", ""))
        for item in tradable + bucket_items
        if str(item.get("sector", "")).strip() and item.get("trade_language_allowed")
    }
    tradable_order = sorted(tradable_sectors, key=lambda s: -sector_scores.get(s, 0.0))
    watch_sectors = {
        str(item.get("sector", ""))
        for item in weak_pool + watchlist
        if str(item.get("sector", "")).strip() and str(item.get("sector", "")) not in tradable_sectors
    }
    watch_order = sorted(watch_sectors, key=lambda s: -sector_scores.get(s, 0.0))
    return tradable_order[:3], watch_order[:3]


def _board_sections(items: list[dict[str, Any]], session: str) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        board = _resolve_a_stock_board(
            str(item.get("symbol", "")),
            str(item.get("market_board", "")) if item.get("market_board") else None,
            str(item.get("market", "")),
        )
        groups.setdefault(board, []).append(item)
        item["_resolved_board"] = board

    keys = sorted(groups.keys(), key=lambda k: _board_key(k))
    ordered: list[tuple[str, list[dict[str, Any]]]] = []
    for key in keys:
        ordered_items = sorted(groups[key], key=lambda item: float(item.get("execution_score", 0.0) or 0.0), reverse=True)
        title = "今日可做" if any(item.get("trade_language_allowed") for item in ordered_items) else "观察池"
        ordered.append((f"{title}｜{key}", ordered_items))
    return ordered


def _bucket_line(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "company_name": item.get("company_name"),
        "sector": item.get("sector"),
        "trade_style": _bucket_trade_style(item),
        "current_price": item.get("current_price"),
        "ret_5d": item.get("ret_5d"),
        "execution_score": float(item.get("execution_score", 0.0) or 0.0),
        "push_decision": item.get("push_decision"),
        "reason_codes": item.get("reason_codes", []),
        "reason": _item_reason(item),
        "pool": item.get("pool", ""),
        "three_locks": item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {},
        "market_board": item.get("market_board"),
        "company_concept": item.get("company_concept"),
        "ai_relationship": item.get("ai_relationship"),
        "concept_verified": item.get("concept_verified"),
        "concept_status": item.get("concept_status"),
        "market_cap_ok": item.get("market_cap_ok"),
        "market_cap_cny_billion": item.get("market_cap_cny_billion"),
        "daily_allowed": item.get("daily_allowed"),
        "intraday_triggered": item.get("intraday_triggered"),
        "fresh_data": item.get("fresh_data"),
        "risk_levels_complete": item.get("risk_levels_complete"),
        "trade_language_allowed": item.get("trade_language_allowed"),
        "trade_levels": item.get("trade_levels", {}),
        "target_plan": item.get("target_plan", {}),
    }


def _item_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("market", "")), str(item.get("symbol", "")))


def _execution_score(item: dict[str, Any]) -> float:
    return float(item.get("execution_score", item.get("rotation_score", item.get("priority_score", 0.0))) or 0.0)


def _certainty_score(item: dict[str, Any]) -> float:
    score = _execution_score(item)
    locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    score += float(locks.get("score", 0.0) or 0.0) * 0.12
    if item.get("active_sector"):
        score += 6.0
    if "high_atr" in _warnings(item):
        score -= 18.0
    support = locks.get("support_level")
    price = item.get("current_price")
    try:
        if support is not None and price and float(price) > 0:
            support_gap = abs(float(price) - float(support)) / float(price)
            score -= min(support_gap, 0.35) * 45.0
    except (TypeError, ValueError):
        pass
    if _three_locks_status(item) == "triple_lock":
        score += 5.0
    elif _three_locks_status(item) == "double_lock":
        score += 2.0
    elif _three_locks_status(item) == "single_lock":
        score -= 4.0
    elif _three_locks_status(item) == "invalid":
        score -= 20.0
    return score


def _three_locks_status(item: dict[str, Any]) -> str:
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    return str(three_locks.get("status", "insufficient_history"))


def _support_level(item: dict[str, Any]) -> Any:
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    return three_locks.get("support_level")


def _has_breakdown(item: dict[str, Any]) -> bool:
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    invalid_if = set(item.get("invalid_if", []) or [])
    return bool(three_locks.get("breakdown_support")) or "three_locks_support_break" in invalid_if


def _warnings(item: dict[str, Any]) -> set[str]:
    return set(item.get("warning_layer", []) or [])


def _is_ai_rotation_family(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key, ""))
        for key in ("sector", "sector_tags", "chain_group", "company_name")
    )
    return any(keyword in text for keyword in AI_ROTATION_KEYWORDS)


def _is_hard_reject(item: dict[str, Any]) -> bool:
    return item.get("push_decision") == "rejected"


def _is_clean_short_candidate(item: dict[str, Any]) -> bool:
    if _is_hard_reject(item) or item.get("horizon") != "short":
        return False
    if _three_locks_status(item) == "invalid" or _has_breakdown(item):
        return False
    if "high_atr" in _warnings(item):
        return False
    return item.get("push_decision") == "tradable_now"


def _premarket_open_sell_candidate(item: dict[str, Any]) -> bool:
    """Setup A: strength before/open, exit quickly if open does not hold."""
    if not _is_clean_short_candidate(item):
        return False
    if not item.get("active_sector"):
        return False
    ret_5d = float(item.get("ret_5d", 0.0) or 0.0)
    return ret_5d >= 0.05 or _execution_score(item) >= 55.0


def _intraday_dip_candidate(item: dict[str, Any]) -> bool:
    """Setup B: wait for a pullback into support, sell close or next premarket."""
    if _is_hard_reject(item) or item.get("horizon") != "short":
        return False
    if _three_locks_status(item) == "invalid" or _has_breakdown(item):
        return False
    if _support_level(item) is None:
        return False
    if "high_atr" in _warnings(item):
        return False
    if item.get("push_decision") not in {"tradable_now", "watch_only"}:
        return False
    if _execution_score(item) >= 35.0:
        return True
    raw_score = float(item.get("rotation_score", item.get("priority_score", 0.0)) or 0.0)
    return _is_ai_rotation_family(item) and raw_score >= 15.0 and _execution_score(item) >= 4.0


def _radar_candidate(item: dict[str, Any]) -> bool:
    if _is_hard_reject(item) or _has_breakdown(item):
        return False
    if _three_locks_status(item) == "invalid":
        return False
    score = _execution_score(item)
    return score >= RADAR_MIN_SCORE or (bool(item.get("active_sector")) and score >= 25.0)


def _overheat_failure_short_candidate(item: dict[str, Any]) -> bool:
    if _is_hard_reject(item) or item.get("horizon") != "short":
        return False
    ret_5d = float(item.get("ret_5d", 0.0) or 0.0)
    overheat = ret_5d >= 0.22 or "high_atr" in _warnings(item)
    failed_structure = _three_locks_status(item) == "invalid" or _has_breakdown(item)
    return overheat and failed_structure


def _playbook_line(item: dict[str, Any], playbook: str) -> dict[str, Any]:
    line = _bucket_line(item)
    support = _support_level(item)
    if playbook == "premarket_open_sell":
        line["trade_style"] = "强势确认"
        line["reason"] = "盘前/开盘承接强，适合继续盯强弱延续；不延续就降级观察"
    elif playbook == "intraday_dip_reversal":
        line["trade_style"] = "回踩确认"
        if not item.get("active_sector") and _is_ai_rotation_family(item):
            line["trade_style"] = "边缘观察"
            line["reason"] = (
                f"AI 关系不是最核心，只看 {_fmt_price(support)} 附近是否有承接，不追高"
                if support is not None
                else "AI 关系不是最核心，只等盘中承接确认，不追高"
            )
        else:
            line["reason"] = (
                f"盘中回到 {_fmt_price(support)} 附近仍有承接，才升级处理"
                if support is not None
                else "只等盘中回落承接确认，直线拉升不追"
            )
    elif playbook == "overheat_failure_short":
        line["trade_style"] = "过热转弱"
        line["reason"] = "涨幅过大后只看转弱确认，跌破关键日内区间才进入风险观察"
    else:
        if "high_atr" in _warnings(item):
            line["trade_style"] = "高波动雷达"
            line["reason"] = "波动过高，只观察缩量回踩或新结构确认，不列交易级"
        else:
            line["trade_style"] = "雷达"
            line["reason"] = _item_reason(item)
    return line


def _danger_reason(item: dict[str, Any]) -> str:
    reason_codes = list(item.get("reason_codes", []))
    if reason_codes:
        return " / ".join(str(x) for x in reason_codes[:3])
    warnings = list(item.get("warning_layer", []))
    if warnings:
        return " / ".join(str(x) for x in warnings[:3])
    if float(item.get("ret_5d", 0.0) or 0.0) >= 0.25:
        return "短线涨幅过大"
    return "等待结构确认"


def _build_mapping_chain(
    us_rotation: dict[str, Any],
    ah_rotation: dict[str, Any],
    *,
    session: str,
) -> list[dict[str, Any]]:
    del session
    rows = []
    for row in (ah_rotation.get("cross_market_signals", []) or []) + (us_rotation.get("cross_market_signals", []) or []):
        if not isinstance(row, dict):
            continue
        corr = float(row.get("correlation", row.get("score", 0.0)) or 0.0)
        best_lag = row.get("best_lag")
        narrative = str(row.get("narrative", "")).strip()
        if "->" in narrative:
            driver = narrative.split("->", 1)[0].strip()
        else:
            driver = "海外主线"
        rows.append(
            {
                "driver": driver,
                "mapped_asset": row.get("sector") or row.get("peer_name") or row.get("symbol") or "映射链",
                "lag": best_lag,
                "correlation": corr,
                "verified_event": bool(row.get("verified_event", False)),
                "note": narrative or "等待真实交易验证",
            }
        )
    rows.sort(key=lambda item: (not item["verified_event"], -abs(item["correlation"])))
    return rows[:3]


def _build_market_state(
    *,
    leaders: list[str],
    premarket_open_sell: list[dict[str, Any]],
    intraday_dip_reversal: list[dict[str, Any]],
    overheat_failure_short: list[dict[str, Any]],
    radar_watch: list[dict[str, Any]],
    danger_pool: list[dict[str, Any]],
) -> dict[str, Any]:
    trade_items = premarket_open_sell + intraday_dip_reversal
    tradable_count = len(trade_items)
    unique_sectors = {item.get("sector") for item in trade_items + radar_watch if item.get("sector")}
    avg_risk = sum(float(item.get("risk_score", 0.0) or 0.0) for item in trade_items) / max(len(trade_items), 1)
    avg_ret_5d = sum(float(item.get("ret_5d", 0.0) or 0.0) for item in trade_items) / max(len(trade_items), 1)

    if tradable_count == 0:
        regime = "风险偏好下降"
    elif len(unique_sectors) >= 3 and tradable_count >= 2:
        regime = "risk-on 扩散"
    elif avg_risk <= -5.0 or avg_ret_5d >= 0.22:
        regime = "高位拥挤"
    else:
        regime = "主线抱团"

    if tradable_count >= 2 and avg_risk > -4.0:
        mainline_health = "主线健康"
    elif tradable_count >= 1:
        mainline_health = "主线仍在但分化"
    else:
        mainline_health = "主线转弱"

    if len(unique_sectors) >= 3:
        breadth = "宽度扩散"
    elif tradable_count <= 1:
        breadth = "指数强、内部弱"
    else:
        breadth = "主线分化"

    if regime == "risk-on 扩散":
        action_bias = "优先做强，但不追最末端"
    elif regime == "高位拥挤":
        action_bias = "只做确认，不做情绪末端追涨"
    elif tradable_count >= 1:
        action_bias = "等确认后参与"
    else:
        action_bias = "控仓观察"

    summary = (
        f"当前更像{regime}，主线状态为{mainline_health}，宽度表现为{breadth}。"
        f"今日可看：强势延续 {len(premarket_open_sell)} 个，"
        f"回踩承接 {len(intraday_dip_reversal)} 个，"
        f"过热转弱 {len(overheat_failure_short)} 个。"
        f"行动倾向：{action_bias}；不满足触发就不做。"
    )

    return {
        "regime": regime,
        "mainline_health": mainline_health,
        "breadth": breadth,
        "action_bias": action_bias,
        "leader_count": len(leaders),
        "tradable_count": tradable_count,
        "premarket_open_sell_count": len(premarket_open_sell),
        "intraday_dip_reversal_count": len(intraday_dip_reversal),
        "overheat_failure_short_count": len(overheat_failure_short),
        "radar_count": len(radar_watch),
        "avg_risk_score": round(avg_risk, 2),
        "avg_ret_5d": round(avg_ret_5d, 4),
        "summary": summary,
    }


def _build_open_script(
    *,
    session: str,
    premarket_open_sell: list[dict[str, Any]],
    intraday_dip_reversal: list[dict[str, Any]],
    overheat_failure_short: list[dict[str, Any]],
    mapping_chain: list[dict[str, Any]],
    market_state: dict[str, Any],
) -> list[str]:
    strength_leaders = [item.get("symbol") for item in premarket_open_sell[:3] if item.get("symbol")]
    dip_leaders = [item.get("symbol") for item in intraday_dip_reversal[:3] if item.get("symbol")]
    short_leaders = [item.get("symbol") for item in overheat_failure_short[:3] if item.get("symbol")]
    if session == "evening":
        script = [
            f"先看美债 10Y、QQQ/SMH 期货强弱，确认今晚是 {market_state['regime']} 还是继续高位拥挤。",
        ]
        if strength_leaders:
            script.append(f"强势确认只盯 {', '.join(strength_leaders)}：盘前/开盘承接要延续，否则降级观察。")
        if dip_leaders:
            script.append(f"回踩确认只盯 {', '.join(dip_leaders)}：回到支撑附近仍有承接，才继续看。")
        if short_leaders:
            script.append(f"过热转弱只盯 {', '.join(short_leaders)}：高位跌破关键日内区间且相对指数转弱，才进入风险观察。")
        if mapping_chain:
            script.append(f"最后看 {mapping_chain[0]['mapped_asset']} 这条映射链能否被次日 A/H 真实交易，不成立就只保留观察。")
        return script

    if session == "tail_close":
        script = [
            f"尾盘只看承接是否延续，别在 {market_state['regime']} 状态里反向硬做。",
        ]
        if strength_leaders:
            script.append(f"强势延续：{', '.join(strength_leaders)} 14:30 后仍站稳关键位才继续看。")
        if dip_leaders:
            script.append(f"回踩确认：{', '.join(dip_leaders)} 只看尾盘承接，不追高。")
        if short_leaders:
            script.append(f"风险回避：{', '.join(short_leaders)} 转弱就不碰。")
        if mapping_chain:
            script.append(f"最后确认 {mapping_chain[0]['driver']} -> {mapping_chain[0]['mapped_asset']} 的映射是否成立，避免只追概念。")
        return script

    script = [
        f"先看指数与核心板块是否延续 {market_state['mainline_health']}，不要在 {market_state['regime']} 状态里做相反节奏。",
    ]
    if strength_leaders:
        script.append(f"强势确认：{', '.join(strength_leaders)} 只有开盘承接延续才继续看。")
    if dip_leaders:
        script.append(f"回踩确认：{', '.join(dip_leaders)} 只等盘中回到支撑附近且不破，没承接不做。")
    if short_leaders:
        script.append(f"过热转弱：{', '.join(short_leaders)} 只作为风险雷达，不抢在转弱前行动。")
    if mapping_chain:
        script.append(f"最后确认 {mapping_chain[0]['driver']} -> {mapping_chain[0]['mapped_asset']} 的映射是否成立，避免只追概念。")
    return script


def _build_decision_layers(
    *,
    us_rotation: dict[str, Any],
    ah_rotation: dict[str, Any],
    session: str,
    classified: list[dict[str, Any]],
    short_block: list[dict[str, Any]],
    coverage_watch: list[dict[str, Any]],
    swing_block: list[dict[str, Any]],
    tradable_now: list[dict[str, Any]],
    watch_shorts: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    leaders: list[str],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    del short_block, coverage_watch, swing_block, tradable_now, watch_shorts

    rejected_sorted = sorted(
        rejected,
        key=_execution_score,
        reverse=True,
    )
    seen_danger: set[tuple[str, str]] = set()
    danger_pool: list[dict[str, Any]] = []
    for item in rejected_sorted:
        key = _item_key(item)
        if not key[0] or not key[1] or key in seen_danger:
            continue
        reasons = set(item.get("reason_codes", []) or [])
        if "market_out_of_scope" in reasons:
            continue
        if _execution_score(item) < DANGER_MIN_SCORE and not reasons:
            continue
        seen_danger.add(key)
        danger_pool.append(
            {
                "symbol": item.get("symbol"),
                "market": item.get("market"),
                "company_name": item.get("company_name"),
                "sector": item.get("sector"),
                "reason": _danger_reason(item),
                "trade_style": "回避",
                "current_price": item.get("current_price"),
                "ret_5d": item.get("ret_5d"),
                "execution_score": _execution_score(item),
                "market_board": item.get("market_board"),
                "company_concept": item.get("company_concept"),
                "ai_relationship": item.get("ai_relationship"),
                "concept_verified": item.get("concept_verified"),
                "concept_status": item.get("concept_status"),
                "market_cap_ok": item.get("market_cap_ok"),
                "market_cap_cny_billion": item.get("market_cap_cny_billion"),
            }
        )

    candidates = sorted(
        [item for item in classified if item.get("push_decision") != "rejected"],
        key=_certainty_score,
        reverse=True,
    )
    assigned: set[tuple[str, str]] = set()

    def assign(predicate: Any, playbook: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in candidates:
            key = _item_key(item)
            if not key[0] or not key[1] or key in assigned:
                continue
            if predicate(item):
                assigned.add(key)
                rows.append(_playbook_line(item, playbook))
        return rows

    premarket_open_sell = assign(_premarket_open_sell_candidate, "premarket_open_sell")
    intraday_dip_reversal = assign(_intraday_dip_candidate, "intraday_dip_reversal")
    overheat_failure_short = assign(_overheat_failure_short_candidate, "overheat_failure_short")
    radar_watch = assign(_radar_candidate, "radar_watch")

    mapping_chain = _build_mapping_chain(us_rotation, ah_rotation, session=session)
    market_state = _build_market_state(
        leaders=leaders,
        premarket_open_sell=premarket_open_sell,
        intraday_dip_reversal=intraday_dip_reversal,
        overheat_failure_short=overheat_failure_short,
        radar_watch=radar_watch,
        danger_pool=danger_pool,
    )
    open_script = _build_open_script(
        session=session,
        premarket_open_sell=premarket_open_sell,
        intraday_dip_reversal=intraday_dip_reversal,
        overheat_failure_short=overheat_failure_short,
        mapping_chain=mapping_chain,
        market_state=market_state,
    )
    opportunity_buckets = {
        "premarket_open_sell": premarket_open_sell,
        "intraday_dip_reversal": intraday_dip_reversal,
        "overheat_failure_short": overheat_failure_short,
        "radar_watch": radar_watch,
        "danger_pool": danger_pool,
    }
    return market_state, opportunity_buckets, danger_pool, mapping_chain, open_script


def _build_review_payload(date_str: str, session: str) -> dict[str, Any]:
    try:
        refresh_signal_outcomes(review_date=date_str)
        payload = {"recent": build_recent_review_summary(review_date=date_str, days=3)}
        is_friday = datetime.strptime(date_str, "%Y-%m-%d").weekday() == 4
        if session == "evening" and is_friday:
            payload["weekly"] = build_weekly_review_summary(review_date=date_str)
        return payload
    except Exception as exc:
        return {
            "recent": {
                "window": "近3日",
                "signal_count": 0,
                "priced_count": 0,
                "error": str(exc),
            }
        }


def build_brief_payload(date_str: str, session: str = "morning") -> dict[str, Any]:
    if date_str != _today_cst():
        return _status_payload(date_str, session, ["requested_date_not_today"])
    try:
        us = _load_rotation(date_str, "US")
        ah = _load_rotation(date_str, "AH")
    except FileNotFoundError:
        return _status_payload(date_str, session, ["rotation_missing"])
    if "sector_decision" not in us or "sector_decision" not in ah:
        raise RuntimeError("rotation report missing sector_decision contract")
    us_sector = us["sector_decision"]
    ah_sector = ah["sector_decision"]
    market_sector = {"US": us_sector, "CN": ah_sector, "HK": ah_sector}

    # Re-rank using session-aware scores (intraday overlay + overbought filter)
    meta = _session_meta(session)
    focus = meta.get("focus_markets")  # set of market codes, or None = all markets
    all_recs = _candidate_pool_for_session(us, ah, meta)
    earnings_index, earnings_state = _earnings_payload_status(date_str)

    for rec in all_recs:
        if not rec.get("horizon"):
            rec["horizon"] = "swing" if rec.get("pool") == "ambush" else "short"
        rec["_session_score"] = _session_score(rec, session)
        sector_meta = market_sector[rec["market"]]
        leader_scores = {
            row["sector"]: float(row.get("score", 0.0))
            for row in sector_meta.get("leading_sectors", [])
        }
        rec["level1_rotation_regime"] = sector_meta.get("rotation_regime", "noisy")
        rec["level1_sector_score"] = leader_scores.get(rec["sector"], 0.0)
        rec["active_sector"] = bool(
            rec.get("active_sector", rec["sector"] in sector_meta.get("active_sector_ids", []))
        )
        rec["rank_in_sector"] = int(rec.get("rank_in_sector", 999))
        rec["sector_fit_score"] = float(
            rec.get("sector_fit_score", rec.get("rotation_score") or rec.get("priority_score") or 0.0)
        )
        rec.update(
            apply_shortline_enrichment(
                rec,
                session=session,
                earnings_index=earnings_index,
                earnings_state=earnings_state,
            )
        )

    classified: list[dict[str, Any]] = []
    for rec in all_recs:
        active_sector_ids = list(market_sector[rec["market"]].get("active_sector_ids", []))
        classified.append(
            classify_candidate(
                rec,
                session=session,
                trade_date=date_str,
                active_sector_ids=active_sector_ids,
                earnings_index=earnings_index,
                earnings_state=earnings_state,
            )
        )

    tradable_shorts = sorted(
        [r for r in classified if r["push_decision"] == "tradable_now" and r["horizon"] == "short"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )
    watch_shorts = sorted(
        [r for r in classified if r["push_decision"] == "watch_only" and r["horizon"] == "short"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )
    watch_swings = sorted(
        [r for r in classified if r["push_decision"] != "rejected" and r["horizon"] == "swing"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )
    rejected = sorted(
        [r for r in classified if r["push_decision"] == "rejected"],
        key=lambda x: x["execution_score"],
        reverse=True,
    )

    short_block_size = int(meta.get("short_block_size", 5))
    coverage_watch_size = int(meta.get("coverage_watch_size", 3))
    if meta.get("short_block_source") == "watchlist":
        watchlist_pool = tradable_shorts + watch_shorts
        if meta.get("include_swing_in_watchlist", False):
            watchlist_pool += watch_swings
        short_pool = sorted(
            watchlist_pool,
            key=lambda x: float(x.get("execution_score", 0.0)),
            reverse=True,
        )
    else:
        short_pool = tradable_shorts
    short_block = _pick_with_diversity(short_pool, short_block_size)
    short_syms = {item["symbol"] for item in short_block}
    coverage_watch = _pick_with_diversity(
        [item for item in watch_shorts if item["symbol"] not in short_syms],
        coverage_watch_size,
    )
    coverage_syms = {item["symbol"] for item in coverage_watch}
    swing_block = _pick_with_diversity(
        [item for item in watch_swings if item["symbol"] not in short_syms and item["symbol"] not in coverage_syms],
        3,
    )

    leaders: list[str] = []
    for market_code in ("CN", "HK", "US"):
        if focus is not None and market_code not in focus:
            continue
        for row in market_sector[market_code].get("leading_sectors", []):
            if row["sector"] not in leaders:
                leaders.append(row["sector"])
            if len(leaders) >= 3:
                break
        if len(leaders) >= 3:
            break
    signal = (ah["cross_market_signals"] or us["cross_market_signals"] or [{}])[0]
    freshness_manifest = build_freshness_manifest(_freshness_gate_items(classified), session, date_str)
    fresh_gate = _fresh_gate_status(date_str, session, freshness_manifest)
    market_state, opportunity_buckets, danger_pool, mapping_chain, open_script = _build_decision_layers(
        us_rotation=us,
        ah_rotation=ah,
        session=session,
        classified=classified,
        short_block=short_block,
        coverage_watch=coverage_watch,
        swing_block=swing_block,
        tradable_now=tradable_shorts,
        watch_shorts=watch_shorts,
        rejected=rejected,
        leaders=leaders,
    )
    payload = {
        "run_id": str(uuid4()),
        "date": date_str,
        "session": session,
        "leaders": leaders,
        "cross_market_signal": signal,
        "short_block": short_block,
        "swing_block": swing_block,
        "bottleneck_block": [],
        "coverage_watch": coverage_watch,
        "tradable_now": tradable_shorts,
        "watch_only": watch_shorts + watch_swings,
        "rejected": rejected,
        "market_state": market_state,
        "market_sections": {},
        "three_locks_summary": _three_locks_summary(classified),
        "opportunity_buckets": opportunity_buckets,
        "danger_pool": danger_pool,
        "mapping_chain": mapping_chain,
        "open_script": open_script,
        "signal_review": _build_review_payload(date_str, session),
        "freshness_manifest": freshness_manifest,
        "fresh_gate": fresh_gate,
        "status_only": not fresh_gate["ok"],
        "send_status": "ready" if fresh_gate["ok"] else "status_only",
        "contract_version": CONTRACT_VERSION,
    }
    PushPayload.model_validate(payload)
    return payload


def _data_staleness_note() -> str:
    """Return a warning line if candidates.json is missing or was not generated today.

    New pipeline: fetch_all_daily → screen_candidates → candidates.json (with "date" field).
    Stale = generated on a previous date.  Missing = pipeline hasn't run yet.
    """
    try:
        candidates_path = PROJECT_ROOT / "data" / "candidates.json"
        if not candidates_path.exists():
            return "⚠️ 候选股票数据不存在 — 请先运行 fetch_all_daily.py + screen_candidates.py"
        data = json.loads(candidates_path.read_text())
        gen_date = data.get("date", "")
        if gen_date == _today_cst():
            return ""  # fresh
        return f"⚠️ 数据陈旧 — candidates.json 生成于 {gen_date}，建议运行 fetch_all_daily.py 更新"
    except Exception:
        pass
    return ""
def _load_earnings_plays(date_str: str, top_n: int = 3) -> list[dict]:
    """Load earnings plays from data/earnings_plays.json, return top_n for today.

    In calendar-only mode (data_limited=True), show up to top_n+2 extra plays
    since each card is shorter and the calendar itself is useful for planning.
    """
    path = PROJECT_ROOT / "data" / "earnings_plays.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if data.get("date") != date_str:
            return []  # stale — only use same-day data
        plays = data.get("earnings_plays", [])
        # In limited mode, show more plays (cards are compact)
        if plays and plays[0].get("data_limited"):
            return plays[:top_n + 2]
        return plays[:top_n]
    except Exception:
        return []


def _fmt_earnings_history(reactions: list[dict]) -> str:
    """Format last N post-earnings reactions as a compact string.

    e.g.  ▲+9.2%  ▼-3.1%  ▲+14.5%  ▼-0.8%  ▲+6.3%   (newest → oldest)
    """
    if not reactions:
        return "无历史数据"
    parts = []
    for r in reactions:
        pct = r.get("pct", 0)
        arrow = "▲" if pct >= 0 else "▼"
        parts.append(f"{arrow}{pct:+.1%}")
    return "  ".join(parts)


def _fmt_earnings_block(play: dict, idx: int, market_label: dict) -> list[str]:
    """Format one earnings play as Discord lines — v4 financial model.

    Displays:
      Line 1: ticker / name / market / side / earnings date / score
      Line 2: historical reactions + conviction
      Line 3: options-implied move vs historical edge  (options-payoff model)
      Line 4: EPS revision direction + beat rate       (estimate-analysis model)
      Line 5: entry plan (timing / buy zone / SL / T1 T2 / RR)
      Line 6: technical setup note
      Line 7: analyst rating  (optional)
    """
    mkt        = market_label.get(play["market"], play["market"])
    sec        = _sector_cn(play.get("sector", ""))
    side       = play.get("side", "LONG")
    side_emoji = "🟢多" if side == "LONG" else "🔴空"
    cur        = play.get("current_price", 0)
    win        = play.get("win_rate")
    conv       = play.get("conviction", "")
    timing     = play.get("timing", "")
    score      = play.get("total_score", 0)
    days       = play.get("days_to_earnings", 0)
    hist       = _fmt_earnings_history(play.get("historical_reactions", []))

    entry_low  = play.get("entry_low",  cur)
    entry_high = play.get("entry_high", cur)
    sl         = play.get("stop_loss",  cur)
    t1         = play.get("target_1",   cur)
    t2         = play.get("target_2",   cur)
    rr         = play.get("rr",         0)

    # Line 3: implied move edge (options-payoff model)
    implied_label = play.get("implied_label", "")
    if not implied_label:
        impl = play.get("implied_move")
        avg  = play.get("avg_move")
        if impl and avg:
            edge = avg - impl
            implied_label = f"期权隐含±{impl:.1%}  实际均±{avg:.1%}  超额{'+' if edge>=0 else ''}{edge:.1%}"
        elif impl:
            implied_label = f"期权隐含±{impl:.1%}"
        elif avg:
            implied_label = f"实际均±{avg:.1%}（无期权数据）"

    # Line 4: EPS revision (estimate-analysis model)
    rev_dir    = play.get("eps_revision_direction")
    rev_pct    = play.get("eps_revision_pct")
    beat_rate  = play.get("beat_rate")
    avg_surp   = play.get("avg_surprise_pct")
    rev_parts  = []
    if rev_dir and rev_pct is not None:
        arrow = "↑" if rev_dir == "上调" else ("↓" if rev_dir == "下调" else "→")
        rev_parts.append(f"EPS修正:{arrow}{rev_dir}{rev_pct:+.1%}(30日)")
    if beat_rate is not None:
        beat_str = f"过去{round(beat_rate*4)+1}季胜率{beat_rate:.0%}"
        if avg_surp is not None:
            beat_str += f" 均超预期{avg_surp:+.1%}"
        rev_parts.append(beat_str)
    eps_line = "  ".join(rev_parts) if rev_parts else "EPS修正数据不可用"

    # When data is limited (Yahoo Finance blocked), show a simpler calendar-only card.
    if play.get("data_limited"):
        lines = [
            f"#{idx} {play['symbol']} {play['company_name']} [{mkt}·{sec}]"
            f"  📅 财报:{play['earnings_date']}({days}天后) {play.get('release_time','').replace('time-','').replace('-',' ')}",
            f"   预测EPS:{play.get('eps_forecast','N/A')}  上年EPS:{play.get('eps_last_year','N/A')}"
            f"  季度:{play.get('fiscal_quarter','N/A')}",
            f"   ⚠️ 历史/期权/评分数据受限（行情接口不可用，仅展示财报日历）",
        ]
        return lines

    lines = [
        # Line 1: header
        f"#{idx} {play['symbol']} {play['company_name']} [{mkt}·{sec}]"
        f"  {side_emoji}  财报:{play['earnings_date']}({days}天后)  评分:{score:.0f}",
        # Line 2: historical reactions
        f"   历史{len(play.get('historical_reactions', []))}次财报次日: {hist}"
        + (f"  {'多' if side=='LONG' else '空'}胜率:{win:.0%} {conv}" if win is not None else f"  {conv}"),
        # Line 3: options-implied edge  ← options-payoff model
        f"   {implied_label}",
        # Line 4: EPS revision          ← estimate-analysis model
        f"   {eps_line}",
        # Line 5: entry plan
        f"   {timing}  |  买入 {entry_low:.2f}–{entry_high:.2f}  |  SL {sl:.2f}"
        f"  |  T1 {t1:.2f} T2 {t2:.2f}  |  RR 1:{rr:.1f}",
        # Line 6: technical setup
        f"   技术: {play.get('notes', {}).get('technical', '')}",
    ]
    analyst_note = play.get("notes", {}).get("analyst", "")
    if analyst_note:
        lines.append(f"   分析师: {analyst_note}")
    return lines


def _layer_summary(item: dict[str, Any]) -> str:
    parts: list[str] = []
    events = item.get("event_tags", [])
    warnings = item.get("warning_layer", [])
    if events:
        parts.append("事件:" + ",".join(events[:3]))
    if item.get("macro_overlay_score"):
        parts.append(f"宏观:{float(item['macro_overlay_score']):+.1f}")
    risk_score = float(item.get("risk_score", 0.0) or 0.0)
    parts.append(f"风险:{risk_score:+.1f}")
    if warnings:
        parts.append("警报:" + ",".join(warnings[:3]))
    return " | ".join(parts)


def _fmt_bucket_item(item: dict[str, Any], session: str = "morning") -> str:
    mkt = {"CN": "A股", "HK": "港股", "US": "美股"}.get(item.get("market"), item.get("market"))
    sec = _sector_cn(item.get("sector", ""))
    board = item.get("_resolved_board") or item.get("market_board") or f"{mkt}·{sec}"
    name = item.get("company_name") or item.get("symbol")
    reason = str(item.get("reason", "")).strip()
    concept = item.get("company_concept") or sec
    ai_rel = item.get("ai_relationship") or "AI 关系待核验"
    if item.get("trade_language_allowed") and item.get("push_decision") == "tradable_now":
        levels = item.get("trade_levels", {}) if isinstance(item.get("trade_levels"), dict) else {}
        target_line = _fmt_targets(item)
        level_line = f"买入 {_fmt_price(levels.get('buy_level'))}｜确认 {_fmt_price(levels.get('confirm_buy'))}｜加仓 {_fmt_price(levels.get('add_level'))}｜止损 {_fmt_price(levels.get('stop_loss'))}"
        return "\n".join(
            [
                f"{item.get('symbol')} {name} [{board}]",
                f"   {concept}｜AI：{ai_rel}｜15m",
                f"   {level_line}",
                f"   {target_line}",
                f"   周期：{_holding_plan(session)}｜因：{reason}",
            ]
        )

    notes = []
    if item.get("push_decision") == "watch_only":
        notes.append("未触发")
    elif item.get("push_decision") == "rejected":
        notes.append("不做")
    if not item.get("concept_verified"):
        notes.append(f"概念 {item.get('concept_status', '未通过')}")
    if not item.get("market_cap_ok", True):
        notes.append("市值不足")
    note_text = "｜".join(notes)
    suffix = f"｜{note_text}" if note_text else ""
    return (
        f"{item.get('symbol')} {name} [{board}]"
        f"\n   {concept}｜AI：{ai_rel}｜现价 {_fmt_price(item.get('current_price'))}{suffix}"
        f"\n   等：{reason if reason else '新触发'}"
    )


def _append_bucket_lines(lines: list[str], title: str, items: list[dict[str, Any]], max_items: int = 3, session: str = "morning") -> None:
    lines.append("")
    lines.append(f"▌ {title} (共{len(items)}只)")
    if not items:
        lines.append("无")
        return
    for idx, item in enumerate(items[:max_items], start=1):
        rendered = _fmt_bucket_item(item, session=session)
        if not rendered:
            continue
        for line_i, line in enumerate(rendered.splitlines()):
            prefix = f"#{idx} " if line_i == 0 else "   "
            lines.append(f"{prefix}{line}")
    if len(items) > max_items:
        lines.append(f"另 {len(items)-max_items} 只见内部记录。")


def _three_locks_summary(classified: list[dict[str, Any]]) -> str:
    counts = {label: 0 for label in THREE_LOCK_LABELS.values()}
    for item in classified:
        counts[_three_locks_label(item)] = counts.get(_three_locks_label(item), 0) + 1
    useful = [f"{label} {count} 个" for label, count in counts.items() if count]
    return "日线结构：" + "，".join(useful[:5]) if useful else "日线结构：暂无有效结构"


def build_brief_text(date_str: str, session: str = "morning", payload: dict[str, Any] | None = None) -> str:
    payload = payload or build_brief_payload(date_str, session)
    if payload.get("status_only") or not payload.get("fresh_gate", {"ok": True}).get("ok", True):
        return _status_text(date_str, session, payload)

    meta = _session_meta(session)
    leaders = " > ".join(_sector_cn(s) for s in payload["leaders"]) if payload["leaders"] else "无"
    staleness = _data_staleness_note()

    lines = [
        f"{meta['label']} · {date_str}",
    ]
    if staleness:
        lines.append(staleness)

    strong_sectors, watch_sectors = _session_sector_summary(payload)
    lines.append("强势赛道：" + (" > ".join(strong_sectors) if strong_sectors else "暂无交易级确认"))
    if watch_sectors:
        lines.append("弱势赛道有强票：" + " > ".join(watch_sectors))
    elif leaders:
        lines.append(f"重点观察：{leaders}")

    buckets = payload.get("opportunity_buckets", {})
    premarket_open_sell = [item for item in buckets.get("premarket_open_sell", []) if isinstance(item, dict)]
    intraday_dip_reversal = [item for item in buckets.get("intraday_dip_reversal", []) if isinstance(item, dict)]
    overheat_failure_short = [item for item in buckets.get("overheat_failure_short", []) if isinstance(item, dict)]
    candidates = premarket_open_sell + intraday_dip_reversal
    board_sections = _board_sections(candidates, session)
    if board_sections:
        for key, group_items in board_sections:
            _append_bucket_lines(lines, key, group_items, max_items=PLAYBOOK_RENDER_LIMIT, session=session)
    else:
        _append_bucket_lines(lines, "今日可做", candidates, max_items=PLAYBOOK_RENDER_LIMIT, session=session)
    if overheat_failure_short:
        _append_bucket_lines(lines, "风险观察", overheat_failure_short, max_items=DANGER_RENDER_LIMIT, session=session)

    if not premarket_open_sell and not intraday_dip_reversal and not overheat_failure_short:
        tradable_now = [item for item in payload.get("tradable_now", []) if item.get("trade_language_allowed")]
        if tradable_now:
            _append_bucket_lines(lines, "交易级补充", tradable_now, max_items=PLAYBOOK_RENDER_LIMIT)
        elif payload.get("short_block"):
            watchlist = [
                item
                for item in payload.get("short_block", [])
                if item.get("push_decision") == "watch_only"
            ]
            if not watchlist:
                watchlist = payload.get("short_block", [])
            if watchlist:
                _append_bucket_lines(lines, "盯盘候选", watchlist[:PLAYBOOK_RENDER_LIMIT], max_items=PLAYBOOK_RENDER_LIMIT)

    return "\n".join(lines)

def _send_chunk(token: str, channel_id: str, text: str) -> None:
    """POST one message chunk to Discord using requests (handles SSL EOF on Python 3.14+)."""
    url = f"{DISCORD_API_HOST}/channels/{channel_id}/messages"
    payload = {"content": text, "allowed_mentions": {"parse": []}}
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "ai-rotator/1.0",
        },
        data=json.dumps(payload, ensure_ascii=False).encode(),
        verify=certifi.where(),
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Sent message id={result.get('id')}")


def _split_discord_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        next_line = line if not current else current + "\n" + line
        if len(next_line) <= DISCORD_MESSAGE_LIMIT:
            current = next_line
            continue
        if current:
            chunks.append(current)
        if len(line) <= DISCORD_MESSAGE_LIMIT:
            current = line
            continue
        for start in range(0, len(line), DISCORD_MESSAGE_LIMIT):
            chunks.append(line[start:start + DISCORD_MESSAGE_LIMIT])
        current = ""
    if current:
        chunks.append(current)
    return chunks or [""]


def maybe_send(text: str) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        print("[WARN] DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID not set — skipping real send")
        return
    chunks = _split_discord_chunks(text)
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            time.sleep(1.2)
        try:
            _send_chunk(token, channel_id, chunk)
        except requests.HTTPError as exc:
            print(f"[ERROR] Discord send failed: {exc.response.status_code} {exc.response.text[:200]}")
            raise


def _should_send_to_discord(payload: dict[str, Any]) -> bool:
    if payload.get("send_status") == "status_only" or payload.get("status_only"):
        return os.environ.get("AI_ROTATOR_SEND_STATUS_ALERTS", "").strip() == "1"
    return True


def _input_artifact_hash(date_str: str) -> str:
    digest = hashlib.sha256()
    candidates = PROJECT_ROOT / "data" / "candidates.json"
    us_report = PROJECT_ROOT / "reports" / "daily" / f"{date_str}-us-rotation.json"
    ah_report = PROJECT_ROOT / "reports" / "daily" / f"{date_str}-ah-rotation.json"
    earnings = PROJECT_ROOT / "data" / "earnings_plays.json"
    fetch_status = PROJECT_ROOT / "data" / "fetch_status.json"
    session_rules = PROJECT_ROOT / "config" / "session_rules.yaml"
    for path in (candidates, us_report, ah_report, earnings, fetch_status, session_rules):
        if path.exists():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _persist_decision_ledger(payload: dict[str, Any]) -> None:
    artifact_hash = _input_artifact_hash(payload["date"])
    rows: list[dict[str, Any]] = []
    all_items = payload.get("tradable_now", []) + payload.get("watch_only", []) + payload.get("rejected", [])
    for item in all_items:
        row = DecisionLedgerRow.model_validate(
            {
                "run_id": payload["run_id"],
                "trade_date": payload["date"],
                "session": payload["session"],
                "market": item["market"],
                "symbol": item["symbol"],
                "sector": item["sector"],
                "horizon": item.get("horizon", "short"),
                "level1_sector_score": item.get("level1_sector_score"),
                "level1_rotation_regime": item.get("level1_rotation_regime"),
                "level2_rank_in_sector": item.get("rank_in_sector"),
                "level2_sector_fit_score": item.get("sector_fit_score"),
                "level3_execution_score": item.get("execution_score"),
                "push_decision": item.get("push_decision", "rejected"),
                "push_reason": ",".join(item.get("reason_codes", [])) or item.get("push_decision", "rejected"),
                "reject_reason_codes": json.dumps(item.get("reason_codes", []), ensure_ascii=False),
                "contract_version": payload.get("contract_version", CONTRACT_VERSION),
                "input_artifact_hash": artifact_hash,
                "freshness_status": item.get("freshness_status"),
                "catalyst_status": item.get("catalyst_status"),
                "entry_triggered": None,
                "stop_hit": None,
                "target_1_hit": None,
                "target_2_hit": None,
                "mfe_pct": None,
                "mae_pct": None,
                "outcome_1d": None,
                "outcome_2d": None,
                "outcome_5d": None,
            }
        )
        rows.append(row.model_dump())
    insert_decision_ledger(rows)


def _persist_signal_ledger(payload: dict[str, Any]) -> None:
    record_signals_from_payload(payload)


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_today_cst())
    parser.add_argument("--session", default="morning",
                        choices=["morning", "ah_open", "midday", "tail_close", "evening"],
                        help="Which daily session to push (morning/ah_open/midday/tail_close/evening)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = build_brief_payload(args.date, args.session)
    text = build_brief_text(args.date, args.session, payload=payload)
    if args.dry_run:
        print(text)
        return
    if not _session_meta(args.session).get("send_to_discord", True):
        print("[INFO] session configured as no-send warmup; skipping Discord push")
        print(text)
        return
    if _should_send_to_discord(payload):
        maybe_send(text)
    else:
        print("[INFO] status-only payload; skipping Discord push")
    if not payload.get("status_only"):
        _persist_decision_ledger(payload)
        _persist_signal_ledger(payload)
    print(text)


if __name__ == "__main__":
    main()
