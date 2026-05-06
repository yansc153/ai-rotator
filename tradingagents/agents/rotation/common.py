from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = PROJECT_ROOT.parent
CONFIG_DIR = PROJECT_ROOT / "config"
RAW_DIR = WORKSPACE_ROOT / "data" / "raw"
DERIVED_DIR = WORKSPACE_ROOT / "data" / "derived"


@dataclass(frozen=True)
class UniverseSymbol:
    market: str
    symbol: str
    company_name: str
    sector: str
    chain_group: str
    role: str
    priority: int


@dataclass(frozen=True)
class SymbolSnapshot:
    market: str
    symbol: str
    current_price: float
    atr14: float
    atr_pct: float
    turnover5: float
    ret_5d: float
    ret_20d: float
    drawdown_1y: float
    as_of: str


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        return {}
    return data


def load_universe() -> list[UniverseSymbol]:
    rows = load_yaml(CONFIG_DIR / "universe.yaml").get("symbols", [])
    output: list[UniverseSymbol] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        output.append(
            UniverseSymbol(
                market=str(row["market"]).upper(),
                symbol=str(row["symbol"]),
                company_name=str(row["company_name"]),
                sector=str(row["sector"]),
                chain_group=str(row["chain_group"]),
                role=str(row.get("role", "candidate")),
                priority=int(row.get("priority", 50)),
            )
        )
    return output


def load_sector_aliases() -> dict[str, str]:
    return load_yaml(CONFIG_DIR / "sector_aliases.yaml").get("aliases", {})


def load_transmission_rules() -> list[dict[str, Any]]:
    return load_yaml(CONFIG_DIR / "transmission_map.yaml").get("rules", [])


def load_research_json(name: str) -> Any:
    path = DERIVED_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text())


def normalize_symbol_for_file(market: str, symbol: str) -> str:
    if market == "US":
        return symbol
    raw = symbol.split(".")[0]
    if market == "HK":
        return raw.zfill(5)  # SEHK codes are 5-digit zero-padded: "0020" → "00020"
    return raw


def snapshot_for_symbol(item: UniverseSymbol) -> SymbolSnapshot:
    file_key = normalize_symbol_for_file(item.market, item.symbol)
    path = RAW_DIR / f"{item.market}_{file_key}_daily.csv"
    if not path.exists():
        return SymbolSnapshot(
            market=item.market,
            symbol=item.symbol,
            current_price=float(item.priority),
            atr14=max(1.0, item.priority * 0.02),
            atr_pct=0.05,
            turnover5=3.0,
            ret_5d=0.02,
            ret_20d=0.06,
            drawdown_1y=-0.15 if item.role in {"leader", "foundry"} else -0.35,
            as_of=str(date.today()),
        )

    frame = pd.read_csv(path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    for col in frame.columns:
        if col != "date":
            try:
                frame[col] = frame[col].astype(float)
            except (TypeError, ValueError):
                pass
    prev_close = frame["close"].shift(1)
    tr = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr14"] = tr.rolling(14).mean()
    if "turnover" in frame.columns:
        turnover5 = float(frame["turnover"].tail(5).mean())
    else:
        turnover5 = 3.0 if item.market != "US" else 0.0
    last = frame.iloc[-1]
    current = float(last["close"])
    atr14 = float(last["atr14"]) if pd.notna(last["atr14"]) else current * 0.05
    ret_5d = float(frame["close"].iloc[-1] / frame["close"].iloc[-6] - 1) if len(frame) >= 6 else 0.0
    ret_20d = float(frame["close"].iloc[-1] / frame["close"].iloc[-21] - 1) if len(frame) >= 21 else ret_5d
    high_1y = float(frame["high"].tail(252).max()) if len(frame) >= 20 else float(frame["high"].max())
    drawdown = current / high_1y - 1 if high_1y else 0.0
    return SymbolSnapshot(
        market=item.market,
        symbol=item.symbol,
        current_price=current,
        atr14=atr14,
        atr_pct=atr14 / current if current else 0.0,
        turnover5=turnover5,
        ret_5d=ret_5d,
        ret_20d=ret_20d,
        drawdown_1y=drawdown,
        as_of=str(pd.Timestamp(last["date"]).date()),
    )
