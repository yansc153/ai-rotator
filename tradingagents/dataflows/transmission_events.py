from __future__ import annotations

from tradingagents.agents.rotation.common import load_transmission_rules


def evaluate_transmission_rules() -> list[dict]:
    return load_transmission_rules()
