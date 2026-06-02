from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from _common import PROJECT_ROOT, dump_json, load_env_file
from storage.sqlite import insert_recommendations
from tradingagents.contracts.decision_chain import SectorDecision
from tradingagents.agents.rotation.decision_router import build_sector_decision, build_stock_decisions
from tradingagents.agents.rotation.price_engine import PriceEngineConfig, build_short_term_plan, build_swing_plan
from tradingagents.agents.rotation.sector_rotation_agent import create_sector_rotation_agent
from tradingagents.agents.rotation.universe_agent import create_universe_agent


SHORT_CFG = PriceEngineConfig(long_k1=1.6, long_k2=2.6, long_k3=0.8, short_k1=1.2, short_k2=2.2, short_k3=0.6, min_rr=1.5)


def build_rotation(market: str, trade_date: str) -> dict:
    universe_state = create_universe_agent()({"weekly_rotation_top3": []})
    rotation_state = create_sector_rotation_agent()({
        "market": market,
        "trade_date": trade_date,
        "universe_pools": universe_state["universe_pools"],
    })
    sector_decision = build_sector_decision(
        market_scope=market,
        leaders=rotation_state["leading_sectors_today"],
        session="rotation",
    )
    stock_decisions = build_stock_decisions(rotation_state["candidate_set"], sector_decision)
    # Use enriched candidate_set for ambush pool so LLM theses carry through to swings
    enriched_ambush = [r for r in stock_decisions if r.get("pool") == "ambush"]
    if not enriched_ambush:
        enriched_ambush = universe_state["universe_pools"].get("ambush", [])
    recommendations = build_recommendations(
        stock_decisions, enriched_ambush, trade_date, market, sector_decision
    )
    return {
        "trade_date": trade_date,
        "market": market,
        "leading_sectors_today": rotation_state["leading_sectors_today"],
        "fading_sectors_today": rotation_state["fading_sectors_today"],
        "cross_market_signals": rotation_state["cross_market_signals"],
        "transmission_events": rotation_state["transmission_events"],
        "sector_decision": sector_decision.model_dump(),
        "candidate_set": stock_decisions,
        "stock_decisions": stock_decisions,
        "recommendations": recommendations,
    }


def _market_matches(row_market: str, market: str) -> bool:
    if market == "ALL":
        return True
    if market == "AH":
        return row_market in ("CN", "HK")
    return row_market == market


def build_recommendations(
    candidate_set: list[dict],
    ambush_pool: list[dict],
    trade_date: str,
    market: str,
    sector_decision: SectorDecision,
) -> list[dict]:
    shorts: list[dict] = []
    # Short-term: from day_active candidate_set
    for row in candidate_set:
        if not _market_matches(row["market"], market):
            continue
        if row.get("pool") != "day_active":
            continue
        if not row.get("active_sector"):
            continue
        if row.get("sector") not in sector_decision.active_sector_ids:
            continue
        short_plan = build_short_term_plan(
            row["current_price"],
            row["atr_pct"],
            "LONG",
            SHORT_CFG,
            market=row["market"],
            short_filters=[],
        )
        if not short_plan["rejected"]:
            shorts.append({
                **row,
                "side": "LONG",
                "horizon": "short",
                "plan": short_plan,
                "thesis": row.get("llm_thesis") or f"{row['sector']} 动量突破",
                "conviction": min(0.95, 0.55 + row["rotation_score"] / 200),
                "level1_rotation_regime": sector_decision.rotation_regime,
                "level1_sector_score": next(
                    (leader.score for leader in sector_decision.leading_sectors if leader.sector == row["sector"]),
                    0.0,
                ),
            })
    shorts = sorted(shorts, key=lambda item: item["rotation_score"], reverse=True)

    # Swing: only use the ambush pool (stocks down ≥20% from 20d high — left-side entry).
    # If no ambush candidates exist, emit no swing recommendations rather than
    # fabricating a swing block out of day_active momentum names.
    swing_source = [row for row in ambush_pool if _market_matches(row["market"], market)]

    swings: list[dict] = []
    for row in swing_source:
        swing_plan = build_swing_plan(row["current_price"], row["atr14"], market=row["market"])
        if not swing_plan.get("rejected"):
            thesis = row.get("llm_thesis") or f"{row['sector']} 左侧布局"
            conviction = min(0.90, 0.50 + abs(row.get("drawdown_1y", 0)) * 0.5)
            swings.append({
                **row,
                "side": "LONG",
                "horizon": "swing",
                "plan": swing_plan,
                "thesis": thesis,
                "conviction": conviction,
                "level1_rotation_regime": sector_decision.rotation_regime,
                "level1_sector_score": next(
                    (leader.score for leader in sector_decision.leading_sectors if leader.sector == row["sector"]),
                    0.0,
                ),
            })
    swings = sorted(swings, key=lambda item: item["priority_score"], reverse=True)
    run_id = str(uuid4())
    insert_recommendations(
        [
            {
                "run_id": run_id,
                "trade_date": trade_date,
                "market": item["market"],
                "symbol": item["symbol"],
                "company_name": item["company_name"],
                "side": item["side"],
                "horizon": item["horizon"],
                "sector": item["sector"],
                "pool": item["pool"],
                "thesis": item["thesis"],
                "conviction": item["conviction"],
                "current_price": item["current_price"],
                "entry_low": item["plan"].get("entry_low"),
                "entry_high": item["plan"].get("entry_high"),
                "target_1": item["plan"].get("target_1"),
                "target_2": item["plan"].get("target_2"),
                "stop_loss": item["plan"].get("stop_loss"),
                "rr": item["plan"].get("rr"),
                "leading_sector_json": json.dumps(item, ensure_ascii=False),
                "transmission_event_json": json.dumps([], ensure_ascii=False),
                "created_at": trade_date,
            }
            for item in (shorts + swings)
        ]
    )
    return shorts + swings


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", required=True, choices=["US", "AH", "ALL"])
    parser.add_argument("--date", default=str(date.today()))
    args = parser.parse_args()
    payload = build_rotation(args.market, args.date)
    out_path = PROJECT_ROOT / "reports" / "daily" / f"{args.date}-{args.market.lower()}-rotation.json"
    dump_json(out_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
