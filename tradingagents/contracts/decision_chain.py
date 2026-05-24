from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


CONTRACT_VERSION = "2026-05-19-v1"


class SectorLeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sector: str
    market_scope: str
    score: float
    confidence: float = 0.5


class SectorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: str
    market_scope: str
    leading_sectors: list[SectorLeader]
    active_sector_ids: list[str]
    winner_count: int
    active_winner: str | None = None
    rotation_regime: Literal["focused", "broad", "mixed", "noisy"]
    allow_short_term_push: bool
    contract_version: str = CONTRACT_VERSION


class StockDecision(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    market: str
    sector: str
    pool: str
    priority_score: float
    rotation_score: float
    sector_fit_score: float
    rank_in_sector: int
    active_sector: bool
    liquidity_ok: bool = True
    contract_version: str = CONTRACT_VERSION


class FreshnessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: str
    session: str
    intraday_status: Literal["fresh", "missing", "stale", "failed"]
    as_of: str | None = None
    bars_today: int = 0
    source_path: str
    contract_version: str = CONTRACT_VERSION


class ExecutionDecision(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    market: str
    sector: str
    horizon: str
    push_decision: Literal["tradable_now", "watch_only", "rejected"]
    execution_score: float
    reason_codes: list[str] = Field(default_factory=list)
    invalid_if: list[str] = Field(default_factory=list)
    freshness_status: Literal["fresh", "missing", "stale", "failed"]
    catalyst_status: Literal["fresh", "stale", "absent", "data_limited", "not_applicable"]
    active_sector: bool
    rank_in_sector: int | None = None
    sector_fit_score: float | None = None
    contract_version: str = CONTRACT_VERSION


class PushPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    session: str
    leaders: list[str]
    cross_market_signal: dict[str, Any]
    short_block: list[dict[str, Any]]
    swing_block: list[dict[str, Any]]
    coverage_watch: list[dict[str, Any]]
    tradable_now: list[dict[str, Any]]
    watch_only: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    freshness_manifest: list[dict[str, Any]]
    contract_version: str = CONTRACT_VERSION


class DecisionLedgerRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    trade_date: str
    session: str
    market: str
    symbol: str
    sector: str
    horizon: str
    level1_sector_score: float | None = None
    level1_rotation_regime: str | None = None
    level2_rank_in_sector: int | None = None
    level2_sector_fit_score: float | None = None
    level3_execution_score: float | None = None
    push_decision: str
    push_reason: str
    reject_reason_codes: str
    contract_version: str = CONTRACT_VERSION
    input_artifact_hash: str
    freshness_status: str | None = None
    catalyst_status: str | None = None
    entry_triggered: int | None = None
    stop_hit: int | None = None
    target_1_hit: int | None = None
    target_2_hit: int | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    outcome_1d: float | None = None
    outcome_2d: float | None = None
    outcome_5d: float | None = None
