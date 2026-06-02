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


def upsert_signal_ledger(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    ensure_schema()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO signal_ledger (
              run_id, trade_date, session, signal_key, market, symbol, company_name,
              sector, playbook, side, push_price, push_score, three_locks_status,
              three_locks_score, support_level, pressure_level, reason, source_payload_json
            ) VALUES (
              :run_id, :trade_date, :session, :signal_key, :market, :symbol, :company_name,
              :sector, :playbook, :side, :push_price, :push_score, :three_locks_status,
              :three_locks_score, :support_level, :pressure_level, :reason, :source_payload_json
            )
            ON CONFLICT(trade_date, session, market, symbol, playbook) DO UPDATE SET
              run_id = excluded.run_id,
              signal_key = excluded.signal_key,
              company_name = excluded.company_name,
              sector = excluded.sector,
              side = excluded.side,
              push_price = excluded.push_price,
              push_score = excluded.push_score,
              three_locks_status = excluded.three_locks_status,
              three_locks_score = excluded.three_locks_score,
              support_level = excluded.support_level,
              pressure_level = excluded.pressure_level,
              reason = excluded.reason,
              source_payload_json = excluded.source_payload_json
            """,
            rows,
        )
        keys = [
            (row["trade_date"], row["session"], row["market"], row["symbol"], row["playbook"])
            for row in rows
        ]
        out: list[dict[str, Any]] = []
        for key in keys:
            row = conn.execute(
                """
                SELECT * FROM signal_ledger
                WHERE trade_date = ? AND session = ? AND market = ? AND symbol = ? AND playbook = ?
                """,
                key,
            ).fetchone()
            if row:
                out.append(dict(row))
    return out


def list_signal_ledger(
    *,
    since: str | None = None,
    until: str | None = None,
    session: str | None = None,
    include_avoid: bool = False,
) -> list[dict[str, Any]]:
    ensure_schema()
    query = "SELECT * FROM signal_ledger"
    params: list[Any] = []
    clauses: list[str] = []
    if since:
        clauses.append("trade_date >= ?")
        params.append(since)
    if until:
        clauses.append("trade_date <= ?")
        params.append(until)
    if session:
        clauses.append("session = ?")
        params.append(session)
    if not include_avoid:
        clauses.append("side != 'AVOID'")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY trade_date DESC, session DESC, push_score DESC, id DESC"
    with connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def upsert_signal_outcomes(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_schema()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO signal_outcomes (
              signal_id, review_date, current_price, raw_return_pct, trade_return_pct,
              max_price_since_push, min_price_since_push, max_gain_pct, max_drawdown_pct,
              days_since_signal, status
            ) VALUES (
              :signal_id, :review_date, :current_price, :raw_return_pct, :trade_return_pct,
              :max_price_since_push, :min_price_since_push, :max_gain_pct, :max_drawdown_pct,
              :days_since_signal, :status
            )
            ON CONFLICT(signal_id, review_date) DO UPDATE SET
              current_price = excluded.current_price,
              raw_return_pct = excluded.raw_return_pct,
              trade_return_pct = excluded.trade_return_pct,
              max_price_since_push = excluded.max_price_since_push,
              min_price_since_push = excluded.min_price_since_push,
              max_gain_pct = excluded.max_gain_pct,
              max_drawdown_pct = excluded.max_drawdown_pct,
              days_since_signal = excluded.days_since_signal,
              status = excluded.status
            """,
            rows,
        )


def list_latest_signal_outcomes(
    *,
    since: str | None = None,
    until: str | None = None,
    review_date: str | None = None,
    include_avoid: bool = False,
) -> list[dict[str, Any]]:
    ensure_schema()
    params: list[Any] = []
    clauses: list[str] = []
    if since:
        clauses.append("l.trade_date >= ?")
        params.append(since)
    if until:
        clauses.append("l.trade_date <= ?")
        params.append(until)
    if review_date:
        clauses.append("o.review_date = ?")
        params.append(review_date)
    if not include_avoid:
        clauses.append("l.side != 'AVOID'")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    query = f"""
        SELECT
          l.*,
          o.review_date,
          o.current_price,
          o.raw_return_pct,
          o.trade_return_pct,
          o.max_price_since_push,
          o.min_price_since_push,
          o.max_gain_pct,
          o.max_drawdown_pct,
          o.days_since_signal,
          o.status
        FROM signal_ledger l
        JOIN signal_outcomes o ON o.signal_id = l.id
        {where}
        ORDER BY l.trade_date DESC, o.trade_return_pct DESC, l.push_score DESC
    """
    with connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]
