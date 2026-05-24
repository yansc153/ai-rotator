from __future__ import annotations

from collections import defaultdict
from typing import Any

from tradingagents.contracts.decision_chain import SectorDecision, SectorLeader, StockDecision


def _rotation_regime(scores: list[float]) -> str:
    positive = [score for score in scores if score > 0]
    if not positive:
        return "noisy"
    if len(positive) == 1:
        return "focused"
    spread = positive[0] - positive[1]
    if positive[0] >= 0.12 and spread >= 0.03:
        return "focused"
    if len(positive) >= 3 and (max(positive[:3]) - min(positive[:3])) <= 0.02:
        return "broad"
    return "mixed"


def build_sector_decision(
    market_scope: str,
    leaders: list[dict[str, Any]],
    *,
    session: str = "rotation",
    top_n: int = 3,
) -> SectorDecision:
    leader_models = [
        SectorLeader(
            sector=row["sector"],
            market_scope=market_scope,
            score=float(row["score"]),
            confidence=float(row.get("confidence", 0.5)),
        )
        for row in leaders[:top_n]
    ]
    scores = [leader.score for leader in leader_models]
    active_sector_ids = [leader.sector for leader in leader_models if leader.score > 0]
    regime = _rotation_regime(scores)
    return SectorDecision(
        session=session,
        market_scope=market_scope,
        leading_sectors=leader_models,
        active_sector_ids=active_sector_ids,
        winner_count=len(active_sector_ids),
        active_winner=active_sector_ids[0] if active_sector_ids else None,
        rotation_regime=regime,
        allow_short_term_push=bool(active_sector_ids) and regime != "noisy",
    )


def build_stock_decisions(
    candidate_set: list[dict[str, Any]],
    sector_decision: SectorDecision,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidate_set:
        grouped[row["sector"]].append(row)

    for rows in grouped.values():
        rows.sort(key=lambda item: float(item.get("rotation_score", item.get("priority_score", 0))), reverse=True)

    output: list[dict[str, Any]] = []
    for sector, rows in grouped.items():
        for idx, row in enumerate(rows, start=1):
            active_sector = sector in sector_decision.active_sector_ids
            rotation_score = float(row.get("rotation_score", row.get("priority_score", 0)))
            sector_fit_score = rotation_score + (10.0 if active_sector else -10.0)
            liquidity_ok = float(row.get("market_cap", 0.0) or 0.0) > 0
            payload = {
                **row,
                "sector_fit_score": round(sector_fit_score, 4),
                "rank_in_sector": idx,
                "active_sector": active_sector,
                "liquidity_ok": liquidity_ok,
            }
            StockDecision.model_validate(
                {
                    "symbol": payload["symbol"],
                    "market": payload["market"],
                    "sector": payload["sector"],
                    "pool": payload.get("pool", "watch"),
                    "priority_score": float(payload.get("priority_score", 0)),
                    "rotation_score": rotation_score,
                    "sector_fit_score": float(payload["sector_fit_score"]),
                    "rank_in_sector": idx,
                    "active_sector": active_sector,
                    "liquidity_ok": liquidity_ok,
                }
            )
            output.append(payload)

    output.sort(key=lambda row: (not row["active_sector"], -float(row["sector_fit_score"])))
    return output
