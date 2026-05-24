from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any

from .paths import REPO_DATA_DIR


FETCH_MANIFEST_PATH = REPO_DATA_DIR / "fetch_status.json"
_CST = timezone(timedelta(hours=8))


def today_cst() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


def write_fetch_manifest(payload: dict[str, Any]) -> None:
    FETCH_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    FETCH_MANIFEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_fetch_manifest() -> dict[str, Any]:
    if not FETCH_MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(FETCH_MANIFEST_PATH.read_text())
    except Exception:
        return {}
