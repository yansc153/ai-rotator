from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import yaml

from tradingagents.runtime.paths import PROJECT_ROOT


CONFIG_PATH = PROJECT_ROOT / "config" / "bottleneck_watchlist.yaml"
DAILY_CACHE_PATH = PROJECT_ROOT / "data" / "daily_cache.db"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_bottleneck_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"themes": []}
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {"themes": []}


def _source_index(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        market = str(item.get("market", "")).upper()
        symbol = str(item.get("symbol", ""))
        if not market or not symbol:
            continue
        key = (market, symbol)
        current = index.get(key)
        if current is None:
            index[key] = dict(item)
            continue
        old_score = _as_float(current.get("rotation_score") or current.get("priority_score"))
        new_score = _as_float(item.get("rotation_score") or item.get("priority_score"))
        if new_score > old_score:
            index[key] = dict(item)
    return index


def _latest_price_snapshot(market: str, symbol: str, db_path: Path = DAILY_CACHE_PATH) -> dict[str, Any]:
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT date, close
                FROM daily_prices
                WHERE market = ? AND symbol = ?
                ORDER BY date DESC
                LIMIT 21
                """,
                (market, symbol),
            ).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {}
    latest_date, latest_close = rows[0]
    close = _as_float(latest_close)
    ret_5d = 0.0
    ret_20d = 0.0
    if close > 0 and len(rows) >= 6:
        base_5d = _as_float(rows[5][1])
        if base_5d > 0:
            ret_5d = close / base_5d - 1
    if close > 0 and len(rows) >= 21:
        base_20d = _as_float(rows[20][1])
        if base_20d > 0:
            ret_20d = close / base_20d - 1
    return {
        "current_price": close,
        "ret_5d": ret_5d,
        "ret_20d": ret_20d,
        "as_of": latest_date,
    }


def _theme_records(config: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for theme in config.get("themes", []):
        if not isinstance(theme, dict):
            continue
        for symbol in theme.get("symbols", []):
            if isinstance(symbol, dict):
                records.append((theme, symbol))
    return records


def _score_record(theme: dict[str, Any], spec: dict[str, Any], market_data: dict[str, Any], source_item: dict[str, Any]) -> float:
    score = _as_float(spec.get("conviction"), 50.0)
    score += min(len(spec.get("evidence", [])), 5) * 2.5
    score += min(len(spec.get("watch_triggers", [])), 4) * 1.5
    if source_item:
        score += 6.0
        pool = source_item.get("pool")
        if pool == "day_active":
            score += 3.0
        elif pool == "ambush":
            score += 4.0
    ret_20d = _as_float(market_data.get("ret_20d"))
    if ret_20d > 0:
        score += min(ret_20d * 20.0, 4.0)
    ret_5d = _as_float(market_data.get("ret_5d"))
    if ret_5d > 0.35:
        score -= 10.0
    scarcity = str(theme.get("scarcity", "")).lower()
    if scarcity in {"high", "very_high"}:
        score += 3.0
    return round(max(0.0, min(score, 100.0)), 4)


def build_bottleneck_block(
    source_items: list[dict[str, Any]],
    *,
    session: str,
    limit: int,
    focus_markets: set[str] | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    del session
    if limit <= 0:
        return []
    config = config or load_bottleneck_config()
    source_by_key = _source_index(source_items)
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for theme, spec in _theme_records(config):
        market = str(spec.get("market", "")).upper()
        symbol = str(spec.get("symbol", ""))
        if not market or not symbol:
            continue
        if focus_markets is not None and market not in focus_markets:
            continue
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        source_item = source_by_key.get(key, {})
        market_data = {
            **_latest_price_snapshot(market, symbol),
            **{k: source_item.get(k) for k in ("current_price", "ret_5d", "ret_20d", "atr_pct", "pool") if source_item.get(k) is not None},
        }
        score = _score_record(theme, spec, market_data, source_item)
        output.append(
            {
                "symbol": symbol,
                "company_name": spec.get("company_name") or source_item.get("company_name") or symbol,
                "market": market,
                "sector": spec.get("sector") or source_item.get("sector") or theme.get("theme_id", "bottleneck"),
                "horizon": "bottleneck",
                "pool": "bottleneck",
                "push_decision": "watch_only",
                "execution_score": score,
                "bottleneck_score": score,
                "theme_id": theme.get("theme_id", ""),
                "theme": theme.get("theme", ""),
                "bottleneck_cn": theme.get("bottleneck_cn", ""),
                "time_horizon": spec.get("time_horizon") or theme.get("time_horizon", "3-12月"),
                "chain_role": spec.get("chain_role", ""),
                "constraint": spec.get("constraint", theme.get("constraint", "")),
                "why_buy": spec.get("why_buy", ""),
                "hold_reason": spec.get("hold_reason", ""),
                "irreplaceable_role": spec.get("irreplaceable_role", ""),
                "evidence": spec.get("evidence", []),
                "watch_triggers": spec.get("watch_triggers", theme.get("watch_triggers", [])),
                "invalid_if": spec.get("invalid_if", theme.get("invalid_if", [])),
                "reason_codes": ["bottleneck_watch_only"],
                "freshness_status": "not_applicable",
                "catalyst_status": "not_applicable",
                "current_price": market_data.get("current_price"),
                "ret_5d": market_data.get("ret_5d"),
                "ret_20d": market_data.get("ret_20d"),
                "atr_pct": market_data.get("atr_pct"),
                "as_of": market_data.get("as_of"),
                "source_pool": source_item.get("pool", "static_watchlist"),
            }
        )

    output.sort(key=lambda item: float(item["bottleneck_score"]), reverse=True)
    return output[:limit]
