#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from _common import PROJECT_ROOT, load_env_file
from storage.sqlite import list_latest_signal_outcomes
from tradingagents.agents.rotation.signal_review import (
    build_recent_review_summary,
    build_weekly_review_summary,
    refresh_signal_outcomes,
)

_CST = timezone(timedelta(hours=8))


def _today_cst() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


def _fmt_pct(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):+.1%}"
    except (TypeError, ValueError):
        return ""


def _fmt_price(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def _window_bounds(date_str: str, window: str, days: int) -> tuple[str, str]:
    end = datetime.strptime(date_str, "%Y-%m-%d").date()
    if window == "weekly":
        start = end - timedelta(days=end.weekday())
    else:
        start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _summary(date_str: str, window: str, days: int) -> dict[str, Any]:
    if window == "weekly":
        return build_weekly_review_summary(review_date=date_str)
    return build_recent_review_summary(review_date=date_str, days=days)


def _markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# AI Rotation Signal Review: {summary['window']} ({summary['since']} to {summary['until']})",
        "",
        f"- Signals: {summary.get('signal_count', 0)}",
        f"- Priced: {summary.get('priced_count', 0)}",
        f"- Win rate: {_fmt_pct(summary.get('win_rate'))}",
        f"- Average raw return: {_fmt_pct(summary.get('avg_raw_return_pct'))}",
        "",
        "| Date | Session | Symbol | Side | Playbook | Push | Current | Raw Return | Trade Return | Max Gain | Max Drawdown |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("trade_date", "")),
                    str(row.get("session", "")),
                    str(row.get("symbol", "")),
                    str(row.get("side", "")),
                    str(row.get("playbook", "")),
                    _fmt_price(row.get("push_price")),
                    _fmt_price(row.get("current_price")),
                    _fmt_pct(row.get("raw_return_pct")),
                    _fmt_pct(row.get("trade_return_pct")),
                    _fmt_pct(row.get("max_gain_pct")),
                    _fmt_pct(row.get("max_drawdown_pct")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_today_cst())
    parser.add_argument("--window", choices=["recent", "weekly"], default="weekly")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "reports" / "signal_review"))
    args = parser.parse_args()

    refresh_signal_outcomes(review_date=args.date)
    since, until = _window_bounds(args.date, args.window, args.days)
    summary = _summary(args.date, args.window, args.days)
    rows = list_latest_signal_outcomes(since=since, until=until, review_date=args.date, include_avoid=False)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.date}-{args.window}-signal-review"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    md_path.write_text(_markdown(summary, rows), encoding="utf-8")
    json_path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
