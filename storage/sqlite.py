from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from tradingagents.runtime.paths import PROJECT_ROOT, runtime_db_path, ensure_runtime_dirs

SCHEMA_PATH = PROJECT_ROOT / "storage" / "schemas.sql"


def _db_path() -> Path:
    return runtime_db_path()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_runtime_dirs()
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
            DELETE FROM recommendations
            WHERE trade_date = :trade_date
              AND market = :market
              AND symbol = :symbol
              AND horizon = :horizon
            """,
            rows,
        )
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


def insert_decision_ledger(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_schema()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO decision_ledger (
              run_id, trade_date, session, market, symbol, sector, horizon,
              level1_sector_score, level1_rotation_regime, level2_rank_in_sector,
              level2_sector_fit_score, level3_execution_score, push_decision,
              push_reason, reject_reason_codes, contract_version, input_artifact_hash,
              freshness_status, catalyst_status, entry_triggered, stop_hit,
              target_1_hit, target_2_hit, mfe_pct, mae_pct,
              outcome_1d, outcome_2d, outcome_5d
            ) VALUES (
              :run_id, :trade_date, :session, :market, :symbol, :sector, :horizon,
              :level1_sector_score, :level1_rotation_regime, :level2_rank_in_sector,
              :level2_sector_fit_score, :level3_execution_score, :push_decision,
              :push_reason, :reject_reason_codes, :contract_version, :input_artifact_hash,
              :freshness_status, :catalyst_status, :entry_triggered, :stop_hit,
              :target_1_hit, :target_2_hit, :mfe_pct, :mae_pct,
              :outcome_1d, :outcome_2d, :outcome_5d
            )
            """,
            rows,
        )


def list_decision_ledger(trade_date: str | None = None, session: str | None = None) -> list[dict[str, Any]]:
    ensure_schema()
    query = "SELECT * FROM decision_ledger"
    params: list[Any] = []
    clauses: list[str] = []
    if trade_date:
        clauses.append("trade_date = ?")
        params.append(trade_date)
    if session:
        clauses.append("session = ?")
        params.append(session)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"
    with connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]
