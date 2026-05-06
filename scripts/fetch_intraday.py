"""Fetch 1-hour intraday OHLCV bars for screened candidate symbols.

Source of symbols (in priority order):
    1. data/candidates.json  — top 150 from screen_candidates.py (3357-stock scan)
    2. config/universe.yaml  — legacy 30-stock fallback if candidates.json is absent

Saves as:  data/raw/US_NVDA_1h.csv
           data/raw/CN_688256_1h.csv
           data/raw/HK_00020_1h.csv

Run before each Discord brief so session-aware scoring has fresh intraday data.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import PROJECT_ROOT, load_env_file
from tradingagents.agents.rotation.common import normalize_symbol_for_file

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES_JSON = ROOT / "data" / "candidates.json"

RAW_DIR = PROJECT_ROOT.parent / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

_START_DAYS = 7  # pull last 7 calendar days of 1h bars
_AKSHARE_SLEEP = 1.2  # seconds between akshare intraday calls to avoid rate limits


def fetch_us_intraday(symbol: str) -> bool:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    df = ticker.history(period="5d", interval="1h", auto_adjust=True)
    if df.empty:
        return False
    df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]
    # yfinance returns tz-aware index; strip tz for simple CSV storage
    dt_col = "datetime" if "datetime" in df.columns else "date"
    df = df.rename(columns={dt_col: "datetime"})
    df["datetime"] = df["datetime"].astype(str).str[:19]  # "2026-05-06 14:00:00"
    out = RAW_DIR / f"US_{symbol}_1h.csv"
    df[["datetime", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
    print(f"  US {symbol}: {len(df)} 1h bars, latest={df['close'].iloc[-1]:.2f}")
    return True


def fetch_cn_intraday(symbol: str) -> bool:
    import time
    import akshare as ak

    raw = symbol.split(".")[0]
    start = (date.today() - timedelta(days=_START_DAYS)).strftime("%Y-%m-%d")
    end = date.today().strftime("%Y-%m-%d")
    time.sleep(_AKSHARE_SLEEP)
    try:
        df = ak.stock_zh_a_hist_min_em(
            symbol=raw, period="60",
            start_date=start, end_date=end,
            adjust="qfq",
        )
        if df is None or df.empty:
            return False
        df = df.rename(columns={
            "时间": "datetime", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        })
        df["datetime"] = df["datetime"].astype(str)
        out = RAW_DIR / f"CN_{raw}_1h.csv"
        df[["datetime", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
        print(f"  CN {raw}: {len(df)} 1h bars, latest={df['close'].iloc[-1]:.2f}")
        return True
    except Exception as exc:
        print(f"  CN {raw}: FAILED — {exc}")
        return False


def fetch_hk_intraday(symbol: str) -> bool:
    import time
    import akshare as ak

    # akshare needs 5-digit zero-padded codes
    base = normalize_symbol_for_file("HK", symbol)
    start = (date.today() - timedelta(days=_START_DAYS)).strftime("%Y-%m-%d")
    end = date.today().strftime("%Y-%m-%d")
    time.sleep(_AKSHARE_SLEEP)
    try:
        df = ak.stock_hk_hist_min_em(
            symbol=base, period="60",
            start_date=start, end_date=end,
            adjust="qfq",
        )
        if df is None or df.empty:
            return False
        df = df.rename(columns={
            "时间": "datetime", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        })
        df["datetime"] = df["datetime"].astype(str)
        out = RAW_DIR / f"HK_{base}_1h.csv"
        df[["datetime", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
        print(f"  HK {base}: {len(df)} 1h bars, latest={df['close'].iloc[-1]:.2f}")
        return True
    except Exception as exc:
        print(f"  HK {base}: FAILED — {exc}")
        return False


def _load_symbols() -> list[tuple[str, str]]:
    """Return [(market, symbol), …] for intraday fetching.

    Uses candidates.json (top 150 from 3357-stock screen) when available;
    falls back to legacy universe.yaml (30 stocks) otherwise.
    """
    if CANDIDATES_JSON.exists():
        data = json.loads(CANDIDATES_JSON.read_text())
        candidates = data.get("candidates", [])
        print(f"[fetch_intraday] Using candidates.json ({len(candidates)} symbols)")
        return [(c["market"], c["symbol"]) for c in candidates]

    from tradingagents.agents.rotation.common import load_universe
    items = load_universe()
    print(f"[fetch_intraday] candidates.json not found — using universe.yaml ({len(items)} symbols)")
    return [(item.market, item.symbol) for item in items]


def main() -> None:
    load_env_file()
    symbols = _load_symbols()
    ok = fail = 0
    for market, symbol in symbols:
        try:
            if market == "US":
                success = fetch_us_intraday(symbol)
            elif market == "CN":
                success = fetch_cn_intraday(symbol)
            elif market == "HK":
                success = fetch_hk_intraday(symbol)
            else:
                continue
            if success:
                ok += 1
            else:
                fail += 1
        except Exception as exc:
            print(f"  {market} {symbol}: ERROR — {exc}")
            fail += 1
    print(f"\n完成: {ok} 成功 / {fail} 失败")


if __name__ == "__main__":
    main()
