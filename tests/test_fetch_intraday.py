from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys

import pytest


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


def test_load_symbols_filters_us_rth_confirm_to_us(monkeypatch):
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
        result = intraday._load_symbols(session="us_rth_confirm")

    assert result == [("US", "NVDA")]


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


def test_tail_close_fails_fast_when_intraday_source_is_down(monkeypatch, capsys):
    symbols = [("CN", f"{i:06d}.SZ") for i in range(intraday._FRESH_SESSION_FAIL_FAST_AFTER + 2)]
    monkeypatch.setattr(intraday, "_load_symbols", lambda session, max_symbols: symbols)
    monkeypatch.setattr(intraday, "fetch_cn_intraday", lambda symbol: False)
    monkeypatch.setattr(sys, "argv", ["fetch_intraday.py", "--session", "tail_close"])

    with pytest.raises(SystemExit) as exc:
        intraday.main()

    assert exc.value.code == 2
    assert "intraday source unavailable" in capsys.readouterr().out


def test_main_prefers_ipv4_before_fetching(monkeypatch):
    calls = []

    monkeypatch.setattr(intraday, "_prefer_ipv4_for_requests", lambda: calls.append("ipv4"))
    monkeypatch.setattr(intraday, "_load_symbols", lambda session, max_symbols: [])
    monkeypatch.setattr(sys, "argv", ["fetch_intraday.py", "--session", "tail_close"])

    intraday.main()

    assert calls == ["ipv4"]


def test_fetch_cn_intraday_writes_15m_file(monkeypatch, tmp_path):
    captured = {}

    def fake_fetch(**kwargs):
        captured.update(kwargs)
        return [{"datetime": "2026-06-18 14:15:00", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100}]

    monkeypatch.setattr(intraday, "RAW_DIR", tmp_path)
    monkeypatch.setattr(intraday.skill_market_data, "mootdx_cn_bars", fake_fetch)

    assert intraday.fetch_cn_intraday("688256.SH") is True
    assert captured == {"code": "688256", "category": 9, "offset": 120}
    assert (tmp_path / "CN_688256_15m.csv").exists()


def test_fetch_cn_intraday_wraps_source_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_timeout(timeout_s, func, **kwargs):
        captured["timeout_s"] = timeout_s
        return func(**kwargs)

    monkeypatch.setattr(intraday, "RAW_DIR", tmp_path)
    monkeypatch.setattr(intraday, "_run_with_timeout", fake_timeout)
    monkeypatch.setattr(
        intraday.skill_market_data,
        "mootdx_cn_bars",
        lambda **kwargs: [{"datetime": "2026-06-18 14:15:00", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100}],
    )

    assert intraday.fetch_cn_intraday("688256.SH") is True
    assert captured["timeout_s"] == intraday._CNHK_TIMEOUT_S


def test_fetch_cn_intraday_returns_false_when_mootdx_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(intraday, "RAW_DIR", tmp_path)
    monkeypatch.setattr(intraday.skill_market_data, "mootdx_cn_bars", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("mootdx down")))

    assert intraday.fetch_cn_intraday("688256.SH") is False


def test_fetch_hk_intraday_writes_15m_file(monkeypatch, tmp_path):
    captured = {}

    def fake_fetch(symbol, **kwargs):
        captured["symbol"] = symbol
        captured.update(kwargs)
        return [{"datetime": "2026-06-18 14:15:00", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100}]

    monkeypatch.setattr(intraday, "RAW_DIR", tmp_path)
    monkeypatch.setattr(intraday.skill_market_data, "yahoo_chart", fake_fetch)

    assert intraday.fetch_hk_intraday("0020.HK") is True
    assert captured["symbol"] == "0020.HK"
    assert captured["interval"] == "15m"
    assert (tmp_path / "HK_00020_15m.csv").exists()


def test_fetch_hk_intraday_returns_false_when_yahoo_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(intraday, "RAW_DIR", tmp_path)
    monkeypatch.setattr(intraday.skill_market_data, "yahoo_chart", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("yahoo down")))

    assert intraday.fetch_hk_intraday("0020.HK") is False


def test_yahoo_symbol_maps_cn_and_hk_codes():
    assert intraday._yahoo_symbol("CN", "688256.SH") == "688256.SS"
    assert intraday._yahoo_symbol("CN", "300750.SZ") == "300750.SZ"
    assert intraday._yahoo_symbol("HK", "09988.HK") == "9988.HK"
    assert intraday._yahoo_symbol("HK", "0020.HK") == "0020.HK"


def test_fetch_us_intraday_uses_yahoo_chart(monkeypatch, tmp_path):
    captured = {}

    def fake_fetch(symbol, **kwargs):
        captured["symbol"] = symbol
        captured.update(kwargs)
        return [{"datetime": "2026-05-18 09:30:00", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 1000}]

    monkeypatch.setattr(intraday.skill_market_data, "yahoo_chart", fake_fetch)
    monkeypatch.setattr(intraday, "RAW_DIR", tmp_path)
    assert intraday.fetch_us_intraday("NVDA") is True
    assert captured["symbol"] == "NVDA"
    assert captured["timeout"] == intraday._US_TIMEOUT_S
    assert captured["interval"] == "15m"
    assert (tmp_path / "US_NVDA_15m.csv").exists()


def test_can_fetch_us_intraday_returns_false_on_exception(monkeypatch):
    monkeypatch.setattr(intraday.skill_market_data, "yahoo_chart", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("blocked")))
    assert intraday._can_fetch_us_intraday() is False
