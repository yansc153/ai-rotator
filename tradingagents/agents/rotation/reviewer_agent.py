from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from storage.sqlite import (
    insert_outcomes,
    list_recommendations,
    list_reviewed_recommendation_ids,
    write_weekly_review,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Number of calendar days to wait before reviewing each horizon
HORIZON_REVIEW_DAYS = {
    "short": 1,
    "swing": 30,
}


def _next_business_day(d: date, n: int = 1) -> date:
    """Advance date by n business days (Mon-Fri), skipping weekends."""
    result = d
    added = 0
    while added < n:
        result += timedelta(days=1)
        if result.weekday() < 5:  # Mon=0 … Fri=4
            added += 1
    return result


def _fetch_outcome_price(market: str, symbol: str, target_date: str) -> float | None:
    """
    Fetch the close price for a symbol on or just after target_date.
    Returns None if price is unavailable.
    """
    target_dt = pd.Timestamp(target_date)
    window_end = (target_dt + timedelta(days=5)).strftime("%Y-%m-%d")

    try:
        if market == "US":
            import yfinance as yf

            df = yf.Ticker(symbol).history(start=target_date, end=window_end, auto_adjust=True)
            if df.empty:
                return None
            df.index = pd.to_datetime(df.index).tz_localize(None)
            # Use the first available close on or after target_date
            candidates = df[df.index >= target_dt]
            if candidates.empty:
                return None
            return float(candidates["Close"].iloc[0])

        elif market == "CN":
            import akshare as ak

            raw = symbol.split(".")[0]
            start_str = target_date.replace("-", "")
            end_str = window_end.replace("-", "")
            df = ak.stock_zh_a_hist(
                symbol=raw, period="daily", start_date=start_str, end_date=end_str, adjust="qfq"
            )
            if df is None or df.empty:
                return None
            # CN akshare columns: 日期, 开盘, 收盘, 最高, 最低, …
            df.columns = [c.lower() for c in df.columns]
            close_col = next((c for c in df.columns if "收" in c or "close" in c), df.columns[2])
            return float(df[close_col].iloc[0])

        elif market == "HK":
            import akshare as ak

            raw = symbol.split(".")[0].zfill(5)  # SEHK 5-digit zero-padded
            df = ak.stock_hk_daily(symbol=raw, adjust="qfq")
            if df is None or df.empty:
                return None
            df["date"] = pd.to_datetime(df["date"])
            candidates = df[df["date"] >= target_dt]
            if candidates.empty:
                return None
            return float(candidates["close"].iloc[0])

    except Exception as exc:
        print(f"[WARN] fetch_outcome_price({market}, {symbol}, {target_date}): {exc}")

    return None


def create_reviewer_agent(llm: Any = None):
    del llm

    def node(state: dict[str, Any]) -> dict[str, Any]:
        today = date.fromisoformat(state.get("trade_date") or str(date.today()))
        already_reviewed = list_reviewed_recommendation_ids()
        review_outcomes: list[dict[str, Any]] = []

        for row in list_recommendations():
            rec_id = row["id"]
            if rec_id in already_reviewed:
                continue  # Don't double-review

            horizon = row.get("horizon", "short")
            trade_date = date.fromisoformat(row["trade_date"])
            lag_days = HORIZON_REVIEW_DAYS.get(horizon, 1)

            # For short-term: use business days; for swing: calendar days
            if horizon == "short":
                review_date = _next_business_day(trade_date, lag_days)
            else:
                review_date = trade_date + timedelta(days=lag_days)

            if review_date > today:
                continue  # Not yet time to review

            review_date_str = str(review_date)
            close_price = _fetch_outcome_price(row["market"], row["symbol"], review_date_str)

            if close_price is None:
                print(f"[WARN] No outcome price for {row['symbol']} on {review_date_str} — skipping")
                continue

            entry_price = row["current_price"]
            pnl_pct = (close_price - entry_price) / entry_price if entry_price else 0.0

            # Determine thesis validity: did price stay above stop-loss?
            stop_loss = row.get("stop_loss") or 0.0
            if stop_loss and entry_price:
                stop_pct = abs((entry_price - stop_loss) / entry_price)
            else:
                stop_pct = 0.05  # default 5% stop

            thesis_valid = 1 if pnl_pct > -stop_pct else 0
            failure_layer = None
            failure_reason = None
            if pnl_pct < -stop_pct:
                failure_layer = "price"
                failure_reason = f"Breached stop ({pnl_pct:.1%} < -{stop_pct:.1%})"
            elif pnl_pct < 0:
                failure_layer = "momentum"
                failure_reason = f"Negative but within stop ({pnl_pct:.1%})"

            outcome = {
                "recommendation_id": rec_id,
                "review_horizon": f"T+{lag_days}",
                "review_date": review_date_str,
                "close_price": round(close_price, 4),
                "max_favorable_excursion": 0.0,  # TODO: requires intraday OHLC
                "max_adverse_excursion": 0.0,    # TODO: requires intraday OHLC
                "pnl_pct": round(pnl_pct, 6),
                "thesis_valid": thesis_valid,
                "failure_layer": failure_layer,
                "failure_reason": failure_reason,
                "reviewer_patch": (
                    f"Auto T+{lag_days} review: close={close_price:.2f}, "
                    f"entry={entry_price:.2f}, pnl={pnl_pct:.1%}, "
                    f"stop_pct={stop_pct:.1%}"
                ),
            }
            review_outcomes.append(outcome)

        # Persist outcomes to SQLite
        insert_outcomes(review_outcomes)
        print(f"[INFO] Reviewed {len(review_outcomes)} recommendations (skipped {len(already_reviewed)} already reviewed)")
        return {"review_outcomes": review_outcomes}

    return node


def write_weekly_markdown(report_date: str) -> Path:
    from storage.sqlite import list_recommendations
    import sqlite3
    import os

    rows = list_recommendations()

    # Derive worst 5 from recent recs by lowest RR (most cautious/failed)
    worst = sorted(
        [r for r in rows if r.get("rr") is not None],
        key=lambda r: r.get("rr", 99),
    )[:5]

    # Derive best rules from actual outcomes (placeholder until outcomes accumulate)
    best_rules: list[dict[str, Any]] = []
    try:
        from storage.sqlite import _db_path, connect
        with connect() as conn:
            rule_rows = conn.execute(
                """
                SELECT r.sector, AVG(o.pnl_pct) as avg_pnl, COUNT(*) as n
                FROM recommendations r
                JOIN outcomes o ON o.recommendation_id = r.id
                WHERE o.thesis_valid = 1
                GROUP BY r.sector
                ORDER BY avg_pnl DESC
                LIMIT 5
                """
            ).fetchall()
            best_rules = [
                {"rule_id": f"{row[0]}_momentum", "avg_pnl": round(row[1], 4), "n": row[2], "status": "keep"}
                for row in rule_rows
            ]
    except Exception:
        pass  # Outcomes table empty or DB unavailable — best_rules stays []

    total = len(rows)
    reviewed = sum(1 for r in rows if r.get("rr") is not None)
    summary = (
        f"# AI Rotator Weekly Review\n\n"
        f"- report_date: {report_date}\n"
        f"- recommendations_in_db: {total}\n"
        f"- best_rules_identified: {len(best_rules)}\n"
    )

    out_path = PROJECT_ROOT / "reports" / "weekly_review.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(summary)

    from datetime import date, timedelta
    week_end = date.fromisoformat(report_date)
    week_start = week_end - timedelta(days=6)
    write_weekly_review(str(week_start), str(week_end), summary, worst, best_rules)
    return out_path
