from __future__ import annotations

import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tradingagents.runtime.paths import ensure_runtime_dirs


# ── Python 3.14 SSL compatibility patch ──────────────────────────────────────
# Python 3.14 made TLS close_notify mandatory, raising SSLEOFError for servers
# (e.g. East Money, Yahoo Finance) that close the TCP connection without it.
# We patch urllib3's create_urllib3_context (used by requests/akshare/yfinance)
# to set OP_IGNORE_UNEXPECTED_EOF on every SSL context it creates.
def _patch_ssl_eof() -> None:
    flag = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    if not flag:
        return  # Python ≤ 3.13 — not needed

    try:
        import urllib3.util.ssl_ as _u3ssl

        _orig_create = _u3ssl.create_urllib3_context

        def _patched_create(*args: object, **kwargs: object) -> ssl.SSLContext:
            ctx = _orig_create(*args, **kwargs)
            ctx.options |= flag
            return ctx

        _u3ssl.create_urllib3_context = _patched_create  # type: ignore[assignment]
    except Exception:
        pass  # urllib3 not installed or API changed — skip silently


_patch_ssl_eof()
ensure_runtime_dirs()


def load_env_file() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
