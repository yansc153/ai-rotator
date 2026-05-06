from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / os.getenv("AI_ROTATOR_DB_PATH", "storage/ai_rotator.db")
SCHEMA_PATH = PROJECT_ROOT / "storage" / "schemas.sql"


def _db_path() -> Path:
    path = os.getenv("AI_ROTATOR_DB_PATH")
    if path:
        return (PROJECT_ROOT / path).resolve() if not Path(path).is_absolute() else Path(path)
    return DEFAULT_DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text())


def insert_recommendations(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_schema()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO recommendations (
              run_id, trade_date, market, symbol, company_name, side, horizon, sector, pool,
              thesis, conviction, current_price, entry_low, entry_high, target_1, target_2,
              stop_loss, rr, leading_sector_json, transmission_event_json, created_at
            ) VALUES (
              :run_id, :trade_date, :market, :symbol, :company_name, :side, :horizon, :sector, :pool,
              :thesis, :conviction, :current_price, :entry_low, :entry_high, :target_1, :target_2,
              :stop_loss, :rr, :leading_sector_json, :transmission_event_json, :created_at
            )
            """,
            rows,
        )


def list_recommendations(trade_date: str | None = None) -> list[dict[str, Any]]:
    ensure_schema()
    with connect() as conn:
        if trade_date:
            rows = conn.execute(
                "SELECT * FROM recommendations WHERE trade_date = ? ORDER BY horizon, rr DESC, conviction DESC",
                (trade_date,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recommendations ORDER BY trade_date DESC, horizon, rr DESC, conviction DESC"
            ).fetchall()
    return [dict(row) for row in rows]


def insert_outcomes(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_schema()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO outcomes (
              recommendation_id, review_horizon, review_date, close_price, max_favorable_excursion,
              max_adverse_excursion, pnl_pct, thesis_valid, failure_layer, failure_reason, reviewer_patch
            ) VALUES (
              :recommendation_id, :review_horizon, :review_date, :close_price, :max_favorable_excursion,
              :max_adverse_excursion, :pnl_pct, :thesis_valid, :failure_layer, :failure_reason, :reviewer_patch
            )
            """,
            rows,
        )


def list_reviewed_recommendation_ids() -> set[int]:
    """Return IDs of recommendations that already have at least one outcome row."""
    ensure_schema()
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT recommendation_id FROM outcomes").fetchall()
    return {row[0] for row in rows}


def write_weekly_review(week_start: str, week_end: str, summary_md: str, worst_five: list[dict[str, Any]], best_rules: list[dict[str, Any]]) -> None:
    ensure_schema()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO weekly_reviews (week_start, week_end, summary_md, worst_five_json, best_rules_json, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (week_start, week_end, summary_md, json.dumps(worst_five, ensure_ascii=False), json.dumps(best_rules, ensure_ascii=False)),
        )
