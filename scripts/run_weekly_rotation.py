from __future__ import annotations

import argparse
import json
from datetime import date

from _common import PROJECT_ROOT, dump_json, load_env_file
from tradingagents.agents.rotation.sector_rotation_agent import create_sector_rotation_agent
from tradingagents.agents.rotation.universe_agent import create_universe_agent


def _load_last_weekly_top3() -> list[str]:
    """Read the most recent weekly rotation report to seed sector bonus for this week."""
    report_dir = PROJECT_ROOT / "reports" / "daily"
    weekly_files = sorted(report_dir.glob("*-weekly-rotation.json"), reverse=True)
    if not weekly_files:
        return []
    try:
        data = json.loads(weekly_files[0].read_text())
        return data.get("weekly_rotation_top3", [])
    except Exception:
        return []


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()))
    args = parser.parse_args()
    last_week_top3 = _load_last_weekly_top3()
    universe_state = create_universe_agent()({"weekly_rotation_top3": last_week_top3})
    rotation_state = create_sector_rotation_agent()({
        "market": "ALL",
        "trade_date": args.date,
        "universe_pools": universe_state["universe_pools"],
    })
    payload = {
        "trade_date": args.date,
        "weekly_rotation_top3": [row["sector"] for row in rotation_state["leading_sectors_today"][:3]],
        "watch_pool_top10": rotation_state["candidate_set"][:10],
    }
    out_path = PROJECT_ROOT / "reports" / "daily" / f"{args.date}-weekly-rotation.json"
    dump_json(out_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
