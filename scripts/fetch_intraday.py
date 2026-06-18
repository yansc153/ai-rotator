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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import PROJECT_ROOT, load_env_file
from tradingagents.agents.rotation.common import normalize_symbol_for_file
from tradingagents.data_sources import skill_market_data
from tradingagents.runtime.paths import RAW_DATA_DIR, ensure_runtime_dirs

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES_JSON = ROOT / "data" / "candidates.json"

RAW_DIR = RAW_DATA_DIR
ensure_runtime_dirs()

_US_TIMEOUT_S = 10
_US_PREFLIGHT_TIMEOUT_S = 5
_CNHK_TIMEOUT_S = 15
_FRESH_SESSION_FAIL_FAST_AFTER = 5

SESSION_MARKETS = {
    "morning": {"CN", "HK", "US"},
    "midday": {"CN", "HK", "US"},
    "tail_close": {"CN", "HK"},
    "evening": {"US"},
    "us_rth_confirm": {"US"},
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
    "us_rth_confirm": None,
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


def _write_rows(market: str, output_symbol: str, rows: list[dict], source: str) -> bool:
    import pandas as pd

    if not rows:
        return False
    df = pd.DataFrame(rows).rename(columns={"date": "datetime"})
    required = ["datetime", "open", "high", "low", "close", "volume"]
    if any(col not in df.columns for col in required):
        return False
    out = RAW_DIR / f"{market}_{output_symbol}_15m.csv"
    df[required].to_csv(out, index=False)
    print(f"  {market} {output_symbol}: {len(df)} 15m bars via {source}, latest={df['close'].iloc[-1]:.2f}", flush=True)
    return True


def _can_fetch_us_intraday() -> bool:
    try:
        return bool(skill_market_data.yahoo_chart("AAPL", interval="15m", range_="1d", timeout=_US_PREFLIGHT_TIMEOUT_S))
    except Exception:
        return False


def fetch_us_intraday(symbol: str) -> bool:
    try:
        return _write_rows("US", symbol, skill_market_data.yahoo_chart(symbol, interval="15m", range_="5d", timeout=_US_TIMEOUT_S), "yahoo_chart")
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


def fetch_cn_intraday(symbol: str) -> bool:
    raw = symbol.split(".")[0]
    try:
        rows = _run_with_timeout(_CNHK_TIMEOUT_S, skill_market_data.mootdx_cn_bars, code=raw, category=9, offset=120)
        return _write_rows("CN", raw, rows, "mootdx")
    except Exception as exc:
        print(f"  CN {raw}: FAILED — {exc}", flush=True)
    return False


def fetch_hk_intraday(symbol: str) -> bool:
    base = normalize_symbol_for_file("HK", symbol)
    try:
        return _write_rows("HK", base, skill_market_data.yahoo_chart(_yahoo_symbol("HK", symbol), interval="15m", range_="5d", timeout=_CNHK_TIMEOUT_S), "yahoo_chart")
    except Exception as exc:
        print(f"  HK {base}: FAILED — {exc}", flush=True)
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
    parser.add_argument("--session", choices=["morning", "midday", "tail_close", "evening", "us_rth_confirm"])
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
                if args.session in {"midday", "tail_close", "us_rth_confirm"} and ok == 0 and fail >= _FRESH_SESSION_FAIL_FAST_AFTER:
                    print(
                        "\n[ERROR] intraday source unavailable: "
                        f"{fail} consecutive symbols failed; fresh session cannot continue",
                        flush=True,
                    )
                    sys.exit(2)
        except Exception as exc:
            print(f"  {market} {symbol}: ERROR — {exc}", flush=True)
            fail += 1
            if args.session in {"midday", "tail_close", "us_rth_confirm"} and ok == 0 and fail >= _FRESH_SESSION_FAIL_FAST_AFTER:
                print(
                    "\n[ERROR] intraday source unavailable: "
                    f"{fail} consecutive symbols failed; fresh session cannot continue",
                    flush=True,
                )
                sys.exit(2)
    print(f"\n完成: {ok} 成功 / {fail} 失败", flush=True)


if __name__ == "__main__":
    main()
