from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent


def _workspace_data_dir(project_root: Path, workspace_root: Path) -> Path:
    configured = os.getenv("AI_ROTATOR_DATA_DIR")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (project_root / path).resolve()
    if workspace_root == Path("/"):
        return project_root / "data"
    return workspace_root / "data"


REPO_DATA_DIR = PROJECT_ROOT / "data"
WORKSPACE_DATA_DIR = _workspace_data_dir(PROJECT_ROOT, WORKSPACE_ROOT)
RAW_DATA_DIR = WORKSPACE_DATA_DIR / "raw"
DERIVED_DATA_DIR = WORKSPACE_DATA_DIR / "derived"

REPORTS_DIR = PROJECT_ROOT / "reports"
DAILY_REPORTS_DIR = REPORTS_DIR / "daily"


def runtime_db_path() -> Path:
    configured = os.getenv("AI_ROTATOR_DB_PATH")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    return PROJECT_ROOT / "storage" / "ai_rotator.db"


def ensure_runtime_dirs() -> None:
    REPO_DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DERIVED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    runtime_db_path().parent.mkdir(parents=True, exist_ok=True)
