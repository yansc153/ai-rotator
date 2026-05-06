"""Universe agent: load pre-scored candidates from data/candidates.json.

When screen_candidates.py has run (i.e. data/candidates.json exists), we skip
all the old CSV-based snapshot logic and hand the pre-scored pools straight to
the sector_rotation_agent.  The JSON already contains every field the downstream
pipeline needs (atr14, atr_pct, ret_5d, ret_20d, sector, pool …).

Falls back to the legacy universe.yaml + price-CSV path if candidates.json is
missing — so the system still works during the transition or if the daily batch
fetch hasn't run yet.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ai-rotator/data/candidates.json
_REPO_ROOT = Path(__file__).resolve().parents[3]
CANDIDATES_JSON = _REPO_ROOT / "data" / "candidates.json"


# ── helpers ───────────────────────────────────────────────────────────────────

def _enrich(item: dict[str, Any]) -> dict[str, Any]:
    """Add any missing fields expected by sector_rotation_agent / price_engine."""
    item = dict(item)  # shallow copy — don't mutate the loaded JSON
    # atr14: absolute ATR in price terms, needed by build_swing_plan stop-loss calc
    if "atr14" not in item:
        item["atr14"] = round(item.get("atr_pct", 0.05) * item.get("current_price", 1.0), 4)
    item.setdefault("chain_group", item.get("sector", ""))
    item.setdefault("role", "candidate")
    item.setdefault("drawdown_1y", 0.0)   # 30d cache can't produce 1y drawdown
    item.setdefault("turnover5", 3.0)
    item.setdefault("rotation_score", item.get("priority_score", 0))
    item.setdefault("llm_thesis", "")
    return item


def _pools_from_candidates_json() -> dict[str, list[dict[str, Any]]]:
    """Split candidates.json into {day_active, ambush, watch} pools."""
    data = json.loads(CANDIDATES_JSON.read_text())
    candidates = data.get("candidates", [])
    pools: dict[str, list[dict[str, Any]]] = {"day_active": [], "ambush": [], "watch": []}
    for raw in candidates:
        item = _enrich(raw)
        pool = item.get("pool", "watch")
        pools.setdefault(pool, []).append(item)
    return pools


# ── legacy fallback (universe.yaml + price CSVs) ─────────────────────────────

class _LegacyThresholds:
    """Internal defaults. Use UniverseThresholds for the public API."""
    day_active_atr_pct_min: float = 0.05
    day_active_turnover5_min: float = 2.0
    ambush_drawdown_from_high: float = -0.30


def _make_legacy_candidate(item: Any, pool: str, score: float, snap: Any) -> dict[str, Any]:
    return {
        "market":        item.market,
        "symbol":        item.symbol,
        "company_name":  item.company_name,
        "sector":        item.sector,
        "sector_tags":   item.sector,
        "chain_group":   item.chain_group,
        "role":          item.role,
        "pool":          pool,
        "priority_score":  round(score, 4),
        "rotation_score":  round(score, 4),
        "current_price":   round(snap.current_price, 4),
        "atr14":           round(snap.atr14, 4),
        "atr_pct":         round(snap.atr_pct, 6),
        "turnover5":       round(snap.turnover5, 4),
        "ret_5d":          round(snap.ret_5d, 6),
        "ret_20d":         round(snap.ret_20d, 6),
        "drawdown_1y":     round(snap.drawdown_1y, 6),
        "as_of":           snap.as_of,
        "llm_thesis":      "",
    }


def _legacy_node(state: dict[str, Any]) -> dict[str, Any]:
    from .common import load_universe, snapshot_for_symbol

    t = _LegacyThresholds()
    weekly_top3 = set(state.get("weekly_rotation_top3", []))
    day_active: list[dict[str, Any]] = []
    ambush: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []

    for item in load_universe():
        snap = snapshot_for_symbol(item)
        bonus = 15 if item.sector in weekly_top3 else 0
        base = item.priority + bonus + snap.ret_5d * 100 + snap.ret_20d * 35
        fallback.append(_make_legacy_candidate(item, "ranked", base, snap))

        if snap.atr_pct >= t.day_active_atr_pct_min and snap.turnover5 >= t.day_active_turnover5_min:
            day_active.append(_make_legacy_candidate(item, "day_active", base + 20, snap))
        if snap.drawdown_1y <= t.ambush_drawdown_from_high:
            ambush.append(_make_legacy_candidate(item, "ambush", base + 10, snap))
        if item.role in {"leader", "foundry", "platform", "equipment", "ai_compute_demand"}:
            watch.append(_make_legacy_candidate(item, "watch", base + 5, snap))

    for pool_list in (day_active, ambush, watch, fallback):
        pool_list.sort(key=lambda r: r["priority_score"], reverse=True)

    # Ensure minimum pool sizes by borrowing from fallback
    for pool_list, pool_name, src in [
        (day_active, "day_active", fallback),
        (ambush,     "ambush",     list(reversed(fallback))),
        (watch,      "watch",      fallback),
    ]:
        if len(pool_list) < 5:
            seen = {x["symbol"] for x in pool_list}
            for row in src:
                if row["symbol"] not in seen:
                    pool_list.append({**row, "pool": pool_name})
                    seen.add(row["symbol"])
                if len(pool_list) >= 5:
                    break

    return {"universe_pools": {"day_active": day_active, "ambush": ambush, "watch": watch[:60]}}


# Public alias kept for backward compatibility with __init__.py imports.
UniverseThresholds = _LegacyThresholds


# ── public API ────────────────────────────────────────────────────────────────

def create_universe_agent(thresholds: Any = None):
    """Return the universe node function.

    The ``thresholds`` parameter is kept for backward compatibility but is only
    used by the legacy fallback path (universe.yaml mode).
    """
    def node(state: dict[str, Any]) -> dict[str, Any]:
        if CANDIDATES_JSON.exists():
            pools = _pools_from_candidates_json()
            print(
                f"[universe_agent] candidates.json → "
                f"day_active={len(pools.get('day_active', []))}  "
                f"ambush={len(pools.get('ambush', []))}  "
                f"watch={len(pools.get('watch', []))}"
            )
            return {"universe_pools": pools}

        print("[universe_agent] candidates.json not found — falling back to universe.yaml")
        return _legacy_node(state)

    return node
