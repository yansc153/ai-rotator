from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tradingagents.runtime.llm import generate_json_object, resolve_llm_settings


def test_resolve_llm_settings_from_env():
    with patch.dict(
        "os.environ",
        {
            "AI_ROTATOR_CODEX_BIN": sys.executable,
            "AI_ROTATOR_CODEX_MODEL": "gpt-5-codex",
            "AI_ROTATOR_CODEX_PROFILE": "local",
            "AI_ROTATOR_CODEX_TIMEOUT": "240",
        },
        clear=False,
    ):
        settings = resolve_llm_settings()
    assert settings is not None
    assert settings.bin_path == sys.executable
    assert settings.model == "gpt-5-codex"
    assert settings.profile == "local"
    assert settings.timeout_seconds == 240


def test_generate_json_object_via_codex_exec():
    def fake_run(command: list[str], **kwargs):
        output_path = command[command.index("-o") + 1]
        Path(output_path).write_text('{"ok": true, "runtime": "codex"}')
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with patch.dict(
        "os.environ",
        {
            "AI_ROTATOR_CODEX_BIN": sys.executable,
            "AI_ROTATOR_CODEX_MODEL": "gpt-5-codex",
        },
        clear=False,
    ), patch("tradingagents.runtime.llm.subprocess.run", side_effect=fake_run) as run:
        payload = generate_json_object(system_prompt="system", user_prompt="user")

    assert payload == {"ok": True, "runtime": "codex"}
    called_args = run.call_args.args[0]
    assert called_args[1] == "exec"
    assert "--dangerously-bypass-approvals-and-sandbox" in called_args
    assert "-m" in called_args
    assert "gpt-5-codex" in called_args


def test_generate_json_object_timeout_returns_empty():
    with patch.dict(
        "os.environ",
        {"AI_ROTATOR_CODEX_BIN": sys.executable},
        clear=False,
    ), patch(
        "tradingagents.runtime.llm.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=180),
    ):
        payload = generate_json_object(system_prompt="system", user_prompt="user")
    assert payload == {}
