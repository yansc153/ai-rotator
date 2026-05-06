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
