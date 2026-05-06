from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .common import UniverseSymbol, load_universe, snapshot_for_symbol


@dataclass(frozen=True)
class UniverseThresholds:
    day_active_atr_pct_min: float = 0.05
    day_active_turnover5_min: float = 2.0
    ambush_drawdown_from_high: float = -0.30


def _make_candidate(item: UniverseSymbol, pool: str, score: float, snapshot: Any) -> dict[str, Any]:
    return {
        "market": item.market,
        "symbol": item.symbol,
        "company_name": item.company_name,
        "sector": item.sector,
        "chain_group": item.chain_group,
        "role": item.role,
        "pool": pool,
        "priority_score": round(score, 4),
        "current_price": round(snapshot.current_price, 4),
        "atr14": round(snapshot.atr14, 4),
        "atr_pct": round(snapshot.atr_pct, 6),
        "turnover5": round(snapshot.turnover5, 4),
        "ret_5d": round(snapshot.ret_5d, 6),
        "ret_20d": round(snapshot.ret_20d, 6),
        "drawdown_1y": round(snapshot.drawdown_1y, 6),
        "as_of": snapshot.as_of,
    }


def create_universe_agent(thresholds: UniverseThresholds | None = None):
    thresholds = thresholds or UniverseThresholds()

    def node(state: dict[str, Any]) -> dict[str, Any]:
        weekly_top3 = set(state.get("weekly_rotation_top3", []))
        day_active: list[dict[str, Any]] = []
        ambush: list[dict[str, Any]] = []
        watch: list[dict[str, Any]] = []
        fallback_ranked: list[dict[str, Any]] = []

        for item in load_universe():
            snap = snapshot_for_symbol(item)
            sector_bonus = 15 if item.sector in weekly_top3 else 0
            base_score = item.priority + sector_bonus + snap.ret_5d * 100 + snap.ret_20d * 35
            fallback_ranked.append(_make_candidate(item, "ranked", base_score, snap))

            if snap.atr_pct >= thresholds.day_active_atr_pct_min and snap.turnover5 >= thresholds.day_active_turnover5_min:
                day_active.append(_make_candidate(item, "day_active", base_score + 20, snap))
            if snap.drawdown_1y <= thresholds.ambush_drawdown_from_high:
                ambush.append(_make_candidate(item, "ambush", base_score + 10, snap))
            if item.role in {"leader", "foundry", "platform", "equipment", "ai_compute_demand"}:
                watch.append(_make_candidate(item, "watch", base_score + 5, snap))

        fallback_ranked.sort(key=lambda row: row["priority_score"], reverse=True)
        day_active.sort(key=lambda row: row["priority_score"], reverse=True)
        ambush.sort(key=lambda row: row["priority_score"], reverse=True)
        watch.sort(key=lambda row: row["priority_score"], reverse=True)

        if len(day_active) < 5:
            for row in fallback_ranked:
                if row["symbol"] not in {x["symbol"] for x in day_active}:
                    row = {**row, "pool": "day_active"}
                    day_active.append(row)
                if len(day_active) >= 5:
                    break
        if len(ambush) < 5:
            for row in fallback_ranked[::-1]:
                if row["symbol"] not in {x["symbol"] for x in ambush}:
                    row = {**row, "pool": "ambush"}
                    ambush.append(row)
                if len(ambush) >= 5:
                    break
        if len(watch) < 5:
            for row in fallback_ranked:
                if row["symbol"] not in {x["symbol"] for x in watch}:
                    row = {**row, "pool": "watch"}
                    watch.append(row)
                if len(watch) >= 5:
                    break

        return {
            "universe_pools": {
                "day_active": day_active,
                "ambush": ambush,
                "watch": watch[:60],
            }
        }

    return node
