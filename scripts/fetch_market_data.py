"""Fetch real OHLCV data for all universe symbols and save as CSVs."""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import load_env_file, PROJECT_ROOT
from tradingagents.agents.rotation.common import load_universe

RAW_DIR = PROJECT_ROOT.parent / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

START = (date.today() - timedelta(days=400)).strftime("%Y%m%d")
END = date.today().strftime("%Y%m%d")
START_YF = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")


def fetch_us(symbol: str) -> bool:
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    df = ticker.history(start=START_YF, auto_adjust=True)
    if df.empty:
        return False
    df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={"date": "date", "open": "open", "high": "high",
                             "low": "low", "close": "close", "volume": "volume"})
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    out = RAW_DIR / f"US_{symbol}_daily.csv"
    df[["date", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
    print(f"  US {symbol}: {len(df)} rows, last={df['close'].iloc[-1]:.2f}")
    return True


def fetch_cn(symbol: str) -> bool:
    import akshare as ak
    raw = symbol.split(".")[0]
    try:
        df = ak.stock_zh_a_hist(symbol=raw, period="daily",
                                start_date=START, end_date=END, adjust="qfq")
        if df is None or df.empty:
            return False
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        rename = {"日期": "date", "开盘": "open", "最高": "high",
                  "最低": "low", "收盘": "close", "成交量": "volume",
                  "换手率": "turnover"}
        df = df.rename(columns=rename)
        if "date" not in df.columns and len(df.columns) >= 5:
            df.columns = ["date", "open", "close", "high", "low", "volume",
                          "amount", "amplitude", "pct_change", "change", "turnover"][: len(df.columns)]
        if "date" not in df.columns:
            return False
        df["date"] = df["date"].astype(str)
        cols = [c for c in ["date", "open", "high", "low", "close", "volume", "turnover"] if c in df.columns]
        out = RAW_DIR / f"CN_{raw}_daily.csv"
        df[cols].to_csv(out, index=False)
        last_close = float(df["close"].iloc[-1])
        print(f"  CN {raw}: {len(df)} rows, last={last_close:.2f}")
        return True
    except Exception as exc:
        print(f"  CN {raw}: FAILED — {exc}")
        return False


def fetch_hk(symbol: str) -> bool:
    import akshare as ak
    # symbol is like "0020.HK" or "00981.HK"
    # akshare requires 5-digit zero-padded codes: "0020" → "00020", "00981" stays "00981"
    raw = symbol.split(".")[0]
    base = raw.zfill(5)  # zero-pad to 5 digits to match SEHK format
    try:
        df = ak.stock_hk_daily(symbol=base, adjust="qfq")
        if df is None or df.empty:
            return False
        df["date"] = df["date"].astype(str)
        out = RAW_DIR / f"HK_{base}_daily.csv"
        df[["date", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
        print(f"  HK {base}: {len(df)} rows, last={df['close'].iloc[-1]:.2f}")
        return True
    except Exception as exc:
        print(f"  HK {raw}: FAILED — {exc}")
        return False


def main() -> None:
    load_env_file()
    universe = load_universe()
    ok = fail = 0
    for item in universe:
        try:
            if item.market == "US":
                success = fetch_us(item.symbol)
            elif item.market == "CN":
                success = fetch_cn(item.symbol)
            elif item.market == "HK":
                success = fetch_hk(item.symbol)
            else:
                continue
            if success:
                ok += 1
            else:
                fail += 1
        except Exception as exc:
            print(f"  {item.market} {item.symbol}: ERROR — {exc}")
            fail += 1
    print(f"\n完成: {ok} 成功 / {fail} 失败")


if __name__ == "__main__":
    main()
