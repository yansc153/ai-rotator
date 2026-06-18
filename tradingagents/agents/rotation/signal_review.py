from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from storage.sqlite import (
    list_latest_signal_outcomes,
    list_signal_ledger,
    upsert_signal_ledger,
    upsert_signal_outcomes,
)
from tradingagents.runtime.paths import PROJECT_ROOT

DAILY_CACHE_PATH = PROJECT_ROOT / "data" / "daily_cache.db"

TRADE_PLAYBOOKS = {
    "premarket_open_sell": {"side": "LONG", "label": "主多/开盘强承接"},
    "intraday_dip_reversal": {"side": "LONG", "label": "主多/回踩承接"},
    "overheat_failure_short": {"side": "SHORT", "label": "反手空"},
    "radar_watch": {"side": "LONG", "label": "高波动雷达"},
}
AVOID_PLAYBOOKS = {"danger_pool": {"side": "AVOID", "label": "禁区池"}}


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_str(value: date) -> str:
    return value.isoformat()


def _three_locks(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("three_locks")
    return data if isinstance(data, dict) else {}


def _signal_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key, ""))
        for key in ("trade_date", "session", "market", "symbol", "playbook")
    )


def _price(item: dict[str, Any]) -> float | None:
    for key in ("current_price", "push_price", "price"):
        value = item.get(key)
        if value is None:
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def signal_rows_from_payload(payload: dict[str, Any], *, include_avoid: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    buckets = payload.get("opportunity_buckets", {}) if isinstance(payload.get("opportunity_buckets"), dict) else {}
    specs = dict(TRADE_PLAYBOOKS)
    if include_avoid:
        specs.update(AVOID_PLAYBOOKS)
        buckets = dict(buckets)
        buckets["danger_pool"] = payload.get("danger_pool", [])

    for playbook, spec in specs.items():
        for item in buckets.get(playbook, []) or []:
            if not isinstance(item, dict):
                continue
            if spec["side"] != "AVOID" and item.get("trade_language_allowed") is False:
                continue
            push_price = _price(item)
            if push_price is None:
                continue
            locks = _three_locks(item)
            row = {
                "run_id": payload["run_id"],
                "trade_date": payload["date"],
                "session": payload["session"],
                "market": str(item.get("market", "")),
                "symbol": str(item.get("symbol", "")),
                "company_name": item.get("company_name"),
                "sector": item.get("sector"),
                "playbook": playbook,
                "side": spec["side"],
                "push_price": push_price,
                "push_score": item.get("execution_score"),
                "three_locks_status": locks.get("status"),
                "three_locks_score": locks.get("score"),
                "support_level": locks.get("support_level"),
                "pressure_level": locks.get("pressure_level"),
                "reason": item.get("reason"),
                "source_payload_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
            }
            if not row["market"] or not row["symbol"]:
                continue
            row["signal_key"] = _signal_key(row)
            rows.append(row)
    return rows


def record_signals_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return upsert_signal_ledger(signal_rows_from_payload(payload, include_avoid=False))


def _daily_rows(path: Path, signal: dict[str, Any], review_date: str) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                """
                SELECT date, close, high, low
                FROM daily_prices
                WHERE market = ?
                  AND symbol = ?
                  AND date >= ?
                  AND date <= ?
                ORDER BY date ASC
                """,
                (signal["market"], signal["symbol"], signal["trade_date"], review_date),
            ).fetchall()
        )


def _outcome_for_signal(signal: dict[str, Any], review_date: str, *, cache_path: Path) -> dict[str, Any]:
    rows = _daily_rows(cache_path, signal, review_date)
    days_since = max((_parse_date(review_date) - _parse_date(signal["trade_date"])).days, 0)
    base = {
        "signal_id": signal["id"],
        "review_date": review_date,
        "current_price": None,
        "raw_return_pct": None,
        "trade_return_pct": None,
        "max_price_since_push": None,
        "min_price_since_push": None,
        "max_gain_pct": None,
        "max_drawdown_pct": None,
        "days_since_signal": days_since,
        "status": "no_price",
    }
    if not rows:
        return base

    push_price = float(signal["push_price"])
    latest = rows[-1]
    current_price = float(latest["close"])
    max_price = max(float(row["high"] if row["high"] is not None else row["close"]) for row in rows)
    min_price = min(float(row["low"] if row["low"] is not None else row["close"]) for row in rows)
    raw_return = (current_price - push_price) / push_price
    trade_return = -raw_return if signal.get("side") == "SHORT" else raw_return
    return {
        **base,
        "current_price": current_price,
        "raw_return_pct": raw_return,
        "trade_return_pct": trade_return,
        "max_price_since_push": max_price,
        "min_price_since_push": min_price,
        "max_gain_pct": (max_price - push_price) / push_price,
        "max_drawdown_pct": (min_price - push_price) / push_price,
        "status": "priced",
    }


def refresh_signal_outcomes(
    *,
    review_date: str,
    lookback_days: int = 45,
    cache_path: Path = DAILY_CACHE_PATH,
) -> list[dict[str, Any]]:
    since = _date_str(_parse_date(review_date) - timedelta(days=lookback_days))
    signals = list_signal_ledger(since=since, until=review_date, include_avoid=False)
    outcomes = [_outcome_for_signal(signal, review_date, cache_path=cache_path) for signal in signals]
    upsert_signal_outcomes(outcomes)
    return outcomes


def _summary_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [row for row in rows if row.get("status") == "priced" and row.get("trade_return_pct") is not None]
    if not priced:
        return {
            "signal_count": len(rows),
            "priced_count": 0,
            "win_rate": None,
            "avg_trade_return_pct": None,
            "avg_raw_return_pct": None,
            "top": [],
            "laggard": [],
        }
    top = sorted(priced, key=lambda row: float(row.get("trade_return_pct") or 0), reverse=True)[:3]
    laggard = sorted(priced, key=lambda row: float(row.get("trade_return_pct") or 0))[:2]
    return {
        "signal_count": len(rows),
        "priced_count": len(priced),
        "win_rate": sum(1 for row in priced if float(row.get("trade_return_pct") or 0) > 0) / len(priced),
        "avg_trade_return_pct": sum(float(row.get("trade_return_pct") or 0) for row in priced) / len(priced),
        "avg_raw_return_pct": sum(float(row.get("raw_return_pct") or 0) for row in priced) / len(priced),
        "top": top,
        "laggard": laggard,
    }


def build_recent_review_summary(*, review_date: str, days: int = 3) -> dict[str, Any]:
    since = _date_str(_parse_date(review_date) - timedelta(days=days))
    rows = list_latest_signal_outcomes(since=since, until=review_date, review_date=review_date, include_avoid=False)
    return {
        "window": f"近{days}日",
        "since": since,
        "until": review_date,
        **_summary_from_rows(rows),
    }


def build_weekly_review_summary(*, review_date: str) -> dict[str, Any]:
    end = _parse_date(review_date)
    start = end - timedelta(days=end.weekday())
    rows = list_latest_signal_outcomes(
        since=_date_str(start),
        until=review_date,
        review_date=review_date,
        include_avoid=False,
    )
    return {
        "window": "本周",
        "since": _date_str(start),
        "until": review_date,
        **_summary_from_rows(rows),
    }
