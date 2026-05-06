from __future__ import annotations

from tradingagents.agents.rotation.common import load_research_json


def get_sector_rotation_inputs(trade_date: str) -> dict:
    return {
        "trade_date": trade_date,
        "r1": load_research_json("r1_lead_lag.json") or [],
        "r3": load_research_json("r3_event_study.json") or [],
        "r5_r6": load_research_json("r5_r6_sector_proxy.json") or {},
        "r7_r8": load_research_json("r7_r8_thresholds.json") or {},
    }
