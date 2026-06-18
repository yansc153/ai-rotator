from pathlib import Path

from tradingagents.runtime import paths


def test_workspace_data_dir_uses_repo_data_when_project_parent_is_root(monkeypatch):
    monkeypatch.delenv("AI_ROTATOR_DATA_DIR", raising=False)

    assert paths._workspace_data_dir(Path("/app"), Path("/")) == Path("/app/data")


def test_workspace_data_dir_honors_relative_env_override(monkeypatch):
    monkeypatch.setenv("AI_ROTATOR_DATA_DIR", "runtime-data")

    assert paths._workspace_data_dir(Path("/app"), Path("/")) == Path("/app/runtime-data")
