"""Universe agent: load pre-scored candidates from data/candidates.json."""
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
    candidates = [_enrich(raw) for raw in data.get("candidates", [])]
    pools: dict[str, list[dict[str, Any]]] = {"day_active": [], "ambush": [], "watch": []}
    for item in candidates:
        pool = item.get("pool", "watch")
        pools.setdefault(pool, []).append(item)

    return pools

# ── public API ────────────────────────────────────────────────────────────────

def create_universe_agent(thresholds: Any = None):
    """Return the universe node function."""
    del thresholds

    def node(state: dict[str, Any]) -> dict[str, Any]:
        del state
        if CANDIDATES_JSON.exists():
            pools = _pools_from_candidates_json()
            print(
                f"[universe_agent] candidates.json → "
                f"day_active={len(pools.get('day_active', []))}  "
                f"ambush={len(pools.get('ambush', []))}  "
                f"watch={len(pools.get('watch', []))}"
            )
            return {"universe_pools": pools}

        raise FileNotFoundError(
            f"[universe_agent] candidates.json not found at {CANDIDATES_JSON} — "
            "run fetch_all_daily.py + screen_candidates.py first"
        )

    return node
