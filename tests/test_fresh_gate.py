import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import send_discord_brief as brief


def test_required_intraday_gate_rejects_any_stale_record(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "candidates.json").write_text(json.dumps({"date": "2026-06-18"}))
    monkeypatch.setattr(brief, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(brief, "_today_cst", lambda: "2026-06-18")
    monkeypatch.setattr(brief, "today_cst", lambda: "2026-06-18")
    monkeypatch.setattr(brief, "_session_meta", lambda session: {"require_fresh_intraday": True})
    monkeypatch.setattr(brief, "read_fetch_manifest", lambda: {"trade_date": "2026-06-18", "status": "ok"})

    gate = brief._fresh_gate_status(
        "2026-06-18",
        "tail_close",
        [
            {"symbol": "A", "intraday_status": "fresh"},
            {"symbol": "B", "intraday_status": "stale"},
        ],
    )

    assert gate["ok"] is False
    assert "intraday_not_fresh" in gate["reason_codes"]


def test_fresh_gate_focus_markets_filters_non_focus_records(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "candidates.json").write_text(json.dumps({"date": "2026-06-18"}))
    monkeypatch.setattr(brief, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(brief, "_today_cst", lambda: "2026-06-18")
    monkeypatch.setattr(brief, "today_cst", lambda: "2026-06-18")
    monkeypatch.setattr(
        brief,
        "_session_meta",
        lambda session: {"require_fresh_intraday": True, "focus_markets": {"CN", "HK"}},
    )
    monkeypatch.setattr(brief, "read_fetch_manifest", lambda: {"trade_date": "2026-06-18", "status": "ok"})

    gate = brief._fresh_gate_status(
        "2026-06-18",
        "midday",
        [
            {"symbol": "CN1", "market": "CN", "intraday_status": "fresh"},
            {"symbol": "US1", "market": "US", "intraday_status": "stale"},
        ],
    )

    assert gate["ok"] is True
    assert "intraday_not_fresh" not in gate["reason_codes"]


def test_freshness_gate_items_excludes_rejected_names():
    items = [
        {"symbol": "GOOD", "push_decision": "tradable_now"},
        {"symbol": "WATCH", "push_decision": "watch_only"},
        {"symbol": "BAD", "push_decision": "rejected"},
    ]

    result = brief._freshness_gate_items(items)

    assert [item["symbol"] for item in result] == ["GOOD", "WATCH"]
