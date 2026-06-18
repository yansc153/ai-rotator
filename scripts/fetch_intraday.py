"""Fetch intraday OHLCV bars for screened candidate symbols.

Source of symbols:
    1. data/candidates.json  — top screened universe from screen_candidates.py

Saves as:  data/raw/US_NVDA_15m.csv
           data/raw/CN_688256_15m.csv
           data/raw/HK_00020_15m.csv

Run before each Discord brief so session-aware scoring has fresh intraday data.
"""
from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import PROJECT_ROOT, load_env_file
from tradingagents.agents.rotation.common import normalize_symbol_for_file
from tradingagents.runtime.paths import RAW_DATA_DIR, ensure_runtime_dirs

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES_JSON = ROOT / "data" / "candidates.json"

RAW_DIR = RAW_DATA_DIR
ensure_runtime_dirs()

_START_DAYS = 7  # pull last 7 calendar days of intraday bars
_AKSHARE_SLEEP = 1.2  # seconds between akshare intraday calls to avoid rate limits
_US_TIMEOUT_S = 10
_US_PREFLIGHT_TIMEOUT_S = 5
_CNHK_TIMEOUT_S = 15
_CNHK_RETRIES = 1
_YF_CNHK_TIMEOUT_S = 10
_US_EASTMONEY_PREFIX = "105"
_FRESH_SESSION_FAIL_FAST_AFTER = 5
_PRIMARY_DISABLE_AFTER = 3
_CNHK_PRIMARY_FAILURES = {"CN": 0, "HK": 0}

SESSION_MARKETS = {
    "morning": {"CN", "HK", "US"},
    "midday": {"CN", "HK", "US"},
    "tail_close": {"CN", "HK"},
    "evening": {"US"},
}

# Morning intraday bars are just cache warm-up because intraday_weight=0.
# Sessions with require_fresh_intraday must fetch the full focused candidate
# set; otherwise the fresh gate can never pass after screening produces more
# candidates than this fetcher covers.
SESSION_MAX_SYMBOLS = {
    "morning": 45,
    "midday": None,
    "tail_close": None,
    "evening": 5,
}


def _run_with_timeout(timeout_s: int, func, **kwargs):
    if not hasattr(signal, "SIGALRM"):
        return func(**kwargs)

    def _raise_timeout(_signum, _frame):
        raise TimeoutError(f"intraday source timed out after {timeout_s}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(timeout_s)
    try:
        return func(**kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _prefer_ipv4_for_requests() -> None:
    try:
        import urllib3.util.connection as urllib3_connection
    except Exception:
        return
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


def _primary_is_disabled(market: str) -> bool:
    return _CNHK_PRIMARY_FAILURES.get(market, 0) >= _PRIMARY_DISABLE_AFTER


def _record_primary_failure(market: str) -> None:
    _CNHK_PRIMARY_FAILURES[market] = _CNHK_PRIMARY_FAILURES.get(market, 0) + 1


def _record_primary_success(market: str) -> None:
    _CNHK_PRIMARY_FAILURES[market] = 0


def _can_fetch_us_intraday() -> bool:
    try:
        import akshare as ak

        df = ak.stock_us_hist_min_em(symbol=f"{_US_EASTMONEY_PREFIX}.AAPL")
        return df is not None and not df.empty
    except Exception:
        return False


def fetch_us_intraday(symbol: str) -> bool:
    import akshare as ak
    import yfinance as yf

    eastmoney_symbol = f"{_US_EASTMONEY_PREFIX}.{symbol}"
    try:
        df = ak.stock_us_hist_min_em(symbol=eastmoney_symbol)
        if df is not None and not df.empty:
            df = df.rename(columns={
                "时间": "datetime",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
            })
            df["datetime"] = df["datetime"].astype(str)
            out = RAW_DIR / f"US_{symbol}_15m.csv"
            df[["datetime", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
            print(f"  US {symbol}: {len(df)} 15m bars via eastmoney, latest={df['close'].iloc[-1]:.2f}", flush=True)
            return True
    except Exception as exc:
        print(f"  US {symbol}: eastmoney failed — {exc}", flush=True)

    ticker = yf.Ticker(symbol)
    try:
        df = ticker.history(period="5d", interval="15m", auto_adjust=True, timeout=_US_TIMEOUT_S, prepost=True)
        if df.empty:
            return False
        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        dt_col = "datetime" if "datetime" in df.columns else "date"
        df = df.rename(columns={dt_col: "datetime"})
        df["datetime"] = df["datetime"].astype(str).str[:19]
        out = RAW_DIR / f"US_{symbol}_15m.csv"
        df[["datetime", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
        print(f"  US {symbol}: {len(df)} 15m bars via yfinance, latest={df['close'].iloc[-1]:.2f}", flush=True)
        return True
    except Exception as exc:
        print(f"  US {symbol}: FAILED — {exc}", flush=True)
        return False


def _yahoo_symbol(market: str, symbol: str) -> str:
    if market == "CN":
        raw = symbol.split(".")[0]
        suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
        yahoo_suffix = "SS" if suffix in {"SH", "SS"} or raw.startswith("6") else "SZ"
        return f"{raw}.{yahoo_suffix}"
    if market == "HK":
        base = normalize_symbol_for_file("HK", symbol)
        return f"{base[-4:]}.HK"
    return symbol


def _fetch_yfinance_intraday(market: str, symbol: str, output_symbol: str) -> bool:
    import yfinance as yf

    ticker = yf.Ticker(_yahoo_symbol(market, symbol))
    try:
        df = ticker.history(period="5d", interval="15m", auto_adjust=True, timeout=_YF_CNHK_TIMEOUT_S, prepost=False)
        if df.empty:
            return False
        df = df.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        dt_col = "datetime" if "datetime" in df.columns else "date"
        df = df.rename(columns={dt_col: "datetime"})
        required = ["datetime", "open", "high", "low", "close", "volume"]
        if any(col not in df.columns for col in required):
            return False
        df["datetime"] = df["datetime"].astype(str).str[:19]
        out = RAW_DIR / f"{market}_{output_symbol}_15m.csv"
        df[required].to_csv(out, index=False)
        print(f"  {market} {output_symbol}: {len(df)} 15m bars via yfinance, latest={df['close'].iloc[-1]:.2f}", flush=True)
        return True
    except Exception as exc:
        print(f"  {market} {output_symbol}: yfinance fallback failed — {exc}", flush=True)
        return False


def fetch_cn_intraday(symbol: str) -> bool:
    import time
    import akshare as ak

    raw = symbol.split(".")[0]
    start = (date.today() - timedelta(days=_START_DAYS)).strftime("%Y-%m-%d")
    end = date.today().strftime("%Y-%m-%d")
    last_exc: Exception | None = None
    for attempt in range(1, _CNHK_RETRIES + 1):
        if _primary_is_disabled("CN"):
            last_exc = RuntimeError("eastmoney primary disabled after consecutive failures")
            break
        time.sleep(_AKSHARE_SLEEP)
        try:
            df = _run_with_timeout(
                _CNHK_TIMEOUT_S,
                ak.stock_zh_a_hist_min_em,
                symbol=raw,
                period="15",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
            if df is None or df.empty:
                last_exc = RuntimeError("empty intraday response")
                continue
            df = df.rename(columns={
                "时间": "datetime", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            })
            df["datetime"] = df["datetime"].astype(str)
            out = RAW_DIR / f"CN_{raw}_15m.csv"
            df[["datetime", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
            print(f"  CN {raw}: {len(df)} 15m bars, latest={df['close'].iloc[-1]:.2f}", flush=True)
            _record_primary_success("CN")
            return True
        except Exception as exc:
            last_exc = exc
            _record_primary_failure("CN")
            print(f"  CN {raw}: attempt {attempt}/{_CNHK_RETRIES} failed — {exc}", flush=True)
    if _fetch_yfinance_intraday("CN", symbol, raw):
        return True
    print(f"  CN {raw}: FAILED — {last_exc}", flush=True)
    return False


def fetch_hk_intraday(symbol: str) -> bool:
    import time
    import akshare as ak

    # akshare needs 5-digit zero-padded codes
    base = normalize_symbol_for_file("HK", symbol)
    start = (date.today() - timedelta(days=_START_DAYS)).strftime("%Y-%m-%d")
    end = date.today().strftime("%Y-%m-%d")
    last_exc: Exception | None = None
    for attempt in range(1, _CNHK_RETRIES + 1):
        if _primary_is_disabled("HK"):
            last_exc = RuntimeError("eastmoney primary disabled after consecutive failures")
            break
        time.sleep(_AKSHARE_SLEEP)
        try:
            df = _run_with_timeout(
                _CNHK_TIMEOUT_S,
                ak.stock_hk_hist_min_em,
                symbol=base,
                period="15",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
            if df is None or df.empty:
                last_exc = RuntimeError("empty intraday response")
                continue
            df = df.rename(columns={
                "时间": "datetime", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            })
            df["datetime"] = df["datetime"].astype(str)
            out = RAW_DIR / f"HK_{base}_15m.csv"
            df[["datetime", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
            print(f"  HK {base}: {len(df)} 15m bars, latest={df['close'].iloc[-1]:.2f}", flush=True)
            _record_primary_success("HK")
            return True
        except Exception as exc:
            last_exc = exc
            _record_primary_failure("HK")
            print(f"  HK {base}: attempt {attempt}/{_CNHK_RETRIES} failed — {exc}", flush=True)
    if _fetch_yfinance_intraday("HK", symbol, base):
        return True
    print(f"  HK {base}: FAILED — {last_exc}", flush=True)
    return False


def _load_symbols(session: str | None = None, max_symbols: int | None = None) -> list[tuple[str, str]]:
    """Return [(market, symbol), …] for intraday fetching.
    """
    session_markets = SESSION_MARKETS.get(session) if session else None
    session_limit = max_symbols if max_symbols is not None else SESSION_MAX_SYMBOLS.get(session or "", 0) or None

    if CANDIDATES_JSON.exists():
        data = json.loads(CANDIDATES_JSON.read_text())
        candidates = data.get("candidates", [])
        if session_markets is not None:
            candidates = [c for c in candidates if c["market"] in session_markets]
        if session_limit is not None:
            candidates = candidates[:session_limit]
        print(
            f"[fetch_intraday] Using candidates.json ({len(candidates)} symbols"
            f"{f', session={session}' if session else ''})",
            flush=True,
        )
        return [(c["market"], c["symbol"]) for c in candidates]

    raise FileNotFoundError(
        f"[fetch_intraday] candidates.json not found at {CANDIDATES_JSON} — "
        "run screen_candidates.py first"
    )


def main() -> None:
    _prefer_ipv4_for_requests()
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["morning", "midday", "tail_close", "evening"])
    parser.add_argument("--max-symbols", type=int, default=None)
    args = parser.parse_args()

    symbols = _load_symbols(args.session, args.max_symbols)
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
                if args.session in {"midday", "tail_close"} and ok == 0 and fail >= _FRESH_SESSION_FAIL_FAST_AFTER:
                    print(
                        "\n[ERROR] intraday source unavailable: "
                        f"{fail} consecutive symbols failed; fresh session cannot continue",
                        flush=True,
                    )
                    sys.exit(2)
        except Exception as exc:
            print(f"  {market} {symbol}: ERROR — {exc}", flush=True)
            fail += 1
            if args.session in {"midday", "tail_close"} and ok == 0 and fail >= _FRESH_SESSION_FAIL_FAST_AFTER:
                print(
                    "\n[ERROR] intraday source unavailable: "
                    f"{fail} consecutive symbols failed; fresh session cannot continue",
                    flush=True,
                )
                sys.exit(2)
    print(f"\n完成: {ok} 成功 / {fail} 失败", flush=True)


if __name__ == "__main__":
    main()
