from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_intraday as intraday


def test_load_symbols_filters_evening_to_us(monkeypatch):
    payload = {
        "candidates": [
            {"market": "CN", "symbol": "688256.SH"},
            {"market": "HK", "symbol": "0020.HK"},
            {"market": "US", "symbol": "NVDA"},
            {"market": "US", "symbol": "AMD"},
        ]
    }
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidates.json"
        path.write_text(json.dumps(payload))
        monkeypatch.setattr(intraday, "CANDIDATES_JSON", path)
        result = intraday._load_symbols(session="evening")

    assert result == [("US", "NVDA"), ("US", "AMD")]


def test_load_symbols_filters_tail_close_to_cn_hk(monkeypatch):
    payload = {
        "candidates": [
            {"market": "CN", "symbol": "688256.SH"},
            {"market": "HK", "symbol": "0020.HK"},
            {"market": "US", "symbol": "NVDA"},
        ]
    }
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidates.json"
        path.write_text(json.dumps(payload))
        monkeypatch.setattr(intraday, "CANDIDATES_JSON", path)
        result = intraday._load_symbols(session="tail_close")

    assert result == [("CN", "688256.SH"), ("HK", "0020.HK")]


def test_tail_close_fetches_full_focused_candidate_set(monkeypatch):
    payload = {
        "candidates": [
            {"market": "CN", "symbol": f"{i:06d}.SZ"}
            for i in range(45)
        ]
        + [{"market": "US", "symbol": "NVDA"}]
    }
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidates.json"
        path.write_text(json.dumps(payload))
        monkeypatch.setattr(intraday, "CANDIDATES_JSON", path)
        result = intraday._load_symbols(session="tail_close")

    assert len(result) == 45
    assert all(market == "CN" for market, _ in result)


def test_load_symbols_applies_session_limit(monkeypatch):
    payload = {
        "candidates": [
            {"market": "CN", "symbol": f"S{i:03d}.SH"}
            for i in range(10)
        ]
    }
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidates.json"
        path.write_text(json.dumps(payload))
        monkeypatch.setattr(intraday, "CANDIDATES_JSON", path)
        result = intraday._load_symbols(session="midday", max_symbols=3)

    assert result == [("CN", "S000.SH"), ("CN", "S001.SH"), ("CN", "S002.SH")]


def test_fetch_us_intraday_falls_back_to_yfinance_with_timeout(monkeypatch):
    import pandas as pd
    import akshare as ak
    import yfinance as yf

    captured = {}

    class FakeTicker:
        def history(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            return pd.DataFrame(
                {"Datetime": ["2026-05-18 09:30:00"], "Open": [1.0], "High": [1.1], "Low": [0.9], "Close": [1.05], "Volume": [1000]}
            )

    monkeypatch.setattr(ak, "stock_us_hist_min_em", lambda symbol: (_ for _ in ()).throw(RuntimeError("eastmoney blocked")))
    monkeypatch.setattr(yf, "Ticker", lambda symbol: FakeTicker())
    assert intraday.fetch_us_intraday("NVDA") is True
    assert captured["kwargs"]["timeout"] == intraday._US_TIMEOUT_S


def test_can_fetch_us_intraday_returns_false_on_exception(monkeypatch):
    import requests

    def raise_error(*args, **kwargs):
        raise requests.RequestException("blocked")

    monkeypatch.setattr(requests, "get", raise_error)
    assert intraday._can_fetch_us_intraday() is False
