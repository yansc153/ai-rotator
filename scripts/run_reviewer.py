from __future__ import annotations

import argparse
import json
from datetime import date

from _common import load_env_file
from tradingagents.agents.rotation.reviewer_agent import create_reviewer_agent, write_weekly_markdown


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()))
    args = parser.parse_args()
    payload = create_reviewer_agent()({"trade_date": args.date})
    weekly_path = write_weekly_markdown(args.date)
    print(json.dumps({"review_outcomes": payload["review_outcomes"], "weekly_review": str(weekly_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
