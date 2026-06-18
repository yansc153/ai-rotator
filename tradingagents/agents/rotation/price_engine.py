from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PriceEngineConfig:
    long_k1: float = 1.25
    long_k2: float = 2.0
    long_k3: float = 1.0
    short_k1: float = 1.0
    short_k2: float = 2.0
    short_k3: float = 0.6
    min_rr: float = 1.5


def build_short_term_plan(
    current: float,
    atr_pct: float,
    side: str,
    cfg: PriceEngineConfig | None = None,
    *,
    market: str | None = None,
    short_filters: list[str] | None = None,
    horizon: str = "short",
) -> dict[str, Any]:
    cfg = cfg or PriceEngineConfig()
    market = (market or "").upper()
    side = side.upper()
    short_filters = short_filters or []

    if side == "SHORT" and market == "CN":
        return {"rejected": True, "reject_reason": "cn_market_no_short", "side": side, "horizon": horizon}
    if side == "SHORT" and len(short_filters) < 2:
        return {"rejected": True, "reject_reason": "short_filter_not_met", "side": side, "horizon": horizon}

    if side == "LONG":
        entry_low = current * 0.995
        entry_high = current * 1.003
        target_1 = current * (1 + atr_pct * cfg.long_k1)
        target_2 = current * (1 + atr_pct * cfg.long_k2)
        stop_loss = current * (1 - atr_pct * cfg.long_k3)
        rr = (target_1 - current) / (current - stop_loss) if current != stop_loss else 0.0
    else:
        entry_low = current * 0.997
        entry_high = current * 1.005
        target_1 = current * (1 - atr_pct * cfg.short_k1)
        target_2 = current * (1 - atr_pct * cfg.short_k2)
        stop_loss = current * (1 + atr_pct * cfg.short_k3)
        rr = (current - target_1) / (stop_loss - current) if current != stop_loss else 0.0

    rejected = rr < cfg.min_rr
    return {
        "side": side,
        "horizon": horizon,
        "entry_low": round(entry_low, 4),
        "entry_high": round(entry_high, 4),
        "target_1": round(target_1, 4),
        "target_2": round(target_2, 4),
        "stop_loss": round(stop_loss, 4),
        "rr": round(rr, 4),
        "rejected": rejected,
        "reject_reason": "rr_below_threshold" if rejected else None,
        "short_filters": short_filters,
    }


def build_swing_plan(current: float, atr14: float, *, side: str = "LONG", market: str | None = None) -> dict[str, Any]:
    side = side.upper()
    if side == "SHORT" and (market or "").upper() == "CN":
        return {"rejected": True, "reject_reason": "cn_market_no_short", "side": side, "horizon": "swing"}
    return {
        "side": side,
        "horizon": "swing",
        "entry_tranches": [round(current * 0.99, 4), round(current * 0.92, 4), round(current * 0.85, 4)],
        "weights": [0.4, 0.4, 0.2],
        "stop_loss": round(current - 2 * atr14, 4),
        "target_1": round(current + 6 * atr14, 4),
        "target_2": round(current + 10 * atr14, 4),
        "rejected": False,
        "reject_reason": None,
    }


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _gap_zones_above(item: dict[str, Any], current: float) -> list[tuple[float, float, str]]:
    zones: list[tuple[float, float, str]] = []
    for key in ("fvg_zones", "gap_zones", "upper_gaps"):
        raw = item.get(key, []) or []
        if not isinstance(raw, list):
            continue
        for zone in raw:
            if not isinstance(zone, dict):
                continue
            low = _positive_float(zone.get("lower", zone.get("gap_low", zone.get("low"))))
            high = _positive_float(zone.get("upper", zone.get("gap_high", zone.get("high"))))
            if low is None or high is None:
                continue
            lower, upper = sorted((low, high))
            if upper > current:
                zones.append((lower, upper, str(zone.get("reason") or "上方 FVG/gap")))
    return sorted(zones, key=lambda row: max(row[0], current) - current)


def build_target_plan(item: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic sell/reduce targets with auditable sources.

    Priority:
    1. nearest upper FVG/gap zone
    2. prior high or daily pressure
    3. Fibonacci extension when price is already making highs
    """
    current = _positive_float(item.get("current_price")) or 0.0
    if current <= 0:
        return {"complete": False, "target_source": "unavailable", "targets": []}

    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    atr = _positive_float(item.get("atr14"))
    if atr is None:
        atr_pct = _positive_float(item.get("atr_pct")) or 0.03
        atr = current * atr_pct

    zones = _gap_zones_above(item, current)
    if zones:
        lower, upper, zone_reason = zones[0]
        mid = (lower + upper) / 2
        targets = [
            {"label": "T1", "price": round(lower, 4), "target_source": "fvg_gap", "reason": f"{zone_reason}下沿"},
            {"label": "T2", "price": round(mid, 4), "target_source": "fvg_gap", "reason": f"{zone_reason}中位"},
            {"label": "T3", "price": round(upper, 4), "target_source": "fvg_gap", "reason": f"{zone_reason}上沿"},
        ]
        return {"complete": True, "method": "fvg_gap", "target_source": "fvg_gap", "targets": targets}

    pressure = _positive_float(three_locks.get("pressure_level"))
    high_20d = _positive_float(item.get("high_20d"))
    prior_high = max([value for value in (pressure, high_20d) if value is not None], default=None)
    if prior_high is not None and prior_high > current:
        targets = [
            {"label": "T1", "price": round(prior_high, 4), "target_source": "prior_high", "reason": "前高/日线压力"},
            {"label": "T2", "price": round(prior_high + atr, 4), "target_source": "prior_high", "reason": "压力突破后一倍 ATR"},
            {"label": "T3", "price": round(prior_high + 2 * atr, 4), "target_source": "prior_high", "reason": "压力突破后二倍 ATR"},
        ]
        return {"complete": True, "method": "prior_high", "target_source": "prior_high", "targets": targets}

    support = _positive_float(three_locks.get("support_level"))
    swing_low = _positive_float(item.get("swing_low")) or support or max(current - 3 * atr, current * 0.9)
    swing_high = _positive_float(item.get("swing_high")) or high_20d or current
    pullback_low = _positive_float(item.get("pullback_low")) or support or max(current - atr, swing_low)
    swing_range = max(swing_high - swing_low, atr)
    fib_specs = (("T1", 1.272), ("T2", 1.618), ("T3", 2.0))
    targets = [
        {
            "label": label,
            "price": round(pullback_low + swing_range * ratio, 4),
            "target_source": "fib_extension",
            "reason": f"Fib {ratio:.3f}".rstrip("0").rstrip("."),
        }
        for label, ratio in fib_specs
    ]
    return {"complete": True, "method": "fib_extension", "target_source": "fib_extension", "targets": targets}


def build_trade_level_plan(item: dict[str, Any]) -> dict[str, Any]:
    current = _positive_float(item.get("current_price")) or 0.0
    if current <= 0:
        return {"complete": False, "target_plan": {"complete": False, "target_source": "unavailable", "targets": []}}
    three_locks = item.get("three_locks") if isinstance(item.get("three_locks"), dict) else {}
    support = _positive_float(three_locks.get("support_level"))
    pressure = _positive_float(three_locks.get("pressure_level"))
    atr = _positive_float(item.get("atr14")) or current * (_positive_float(item.get("atr_pct")) or 0.03)
    buy_level = support if support and support < current else current * 0.995
    confirm_buy = pressure if pressure and pressure > current else current + 0.6 * atr
    add_level = max(confirm_buy + 0.35 * atr, current + 0.9 * atr)
    stop_loss = min((support * 0.985) if support else current - atr, buy_level - 0.45 * atr)
    target_plan = build_target_plan(item)
    complete = all(value > 0 for value in (buy_level, confirm_buy, add_level, stop_loss)) and bool(target_plan.get("complete"))
    return {
        "complete": complete,
        "buy_level": round(buy_level, 4),
        "confirm_buy": round(confirm_buy, 4),
        "add_level": round(add_level, 4),
        "stop_loss": round(stop_loss, 4),
        "target_plan": target_plan,
    }


def create_price_engine(cfg: PriceEngineConfig | None = None):
    cfg = cfg or PriceEngineConfig()

    def node(state: dict[str, Any]) -> dict[str, Any]:
        candidate = state.get("selected_candidate") or {}
        side = candidate.get("side", "LONG")
        horizon = candidate.get("horizon", "short")
        market = candidate.get("market", "")
        current = float(candidate.get("current_price", 0))
        atr14 = float(candidate.get("atr14", 0))
        atr_pct = float(candidate.get("atr_pct", atr14 / current if current else 0))
        if horizon == "swing":
            price_plan = build_swing_plan(current, atr14, side=side, market=market)
        else:
            price_plan = build_short_term_plan(
                current,
                atr_pct,
                side,
                cfg,
                market=market,
                short_filters=candidate.get("short_filters", []),
                horizon=horizon,
            )
        return {"price_plan": price_plan}

    return node
