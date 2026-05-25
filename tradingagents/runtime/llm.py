from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT


FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class CodexSettings:
    bin_path: str
    model: str
    timeout_seconds: int
    profile: str


def _is_enabled() -> bool:
    raw = os.getenv("AI_ROTATOR_LLM_ENABLED", "1").strip().lower()
    return raw not in FALSE_VALUES


def _default_codex_bin() -> str:
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    fallback = Path.home() / ".local" / "bin" / "codex"
    return str(fallback)


def resolve_llm_settings() -> CodexSettings | None:
    if not _is_enabled():
        return None

    bin_path = os.getenv("AI_ROTATOR_CODEX_BIN", _default_codex_bin()).strip()
    if not bin_path or not Path(bin_path).exists():
        return None

    return CodexSettings(
        bin_path=bin_path,
        model=os.getenv("AI_ROTATOR_CODEX_MODEL", "").strip(),
        timeout_seconds=int(os.getenv("AI_ROTATOR_CODEX_TIMEOUT", os.getenv("AI_ROTATOR_LLM_TIMEOUT", "180"))),
        profile=os.getenv("AI_ROTATOR_CODEX_PROFILE", "").strip(),
    )


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return {}
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().removeprefix("json").strip()
            try:
                payload = json.loads(part)
            except json.JSONDecodeError:
                continue
            return payload if isinstance(payload, dict) else {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _compose_prompt(system_prompt: str, user_prompt: str) -> str:
    return (
        "Follow the system instructions exactly. "
        "Return only one JSON object and nothing else.\n\n"
        f"<system>\n{system_prompt}\n</system>\n\n"
        f"<user>\n{user_prompt}\n</user>"
    )


def _build_command(settings: CodexSettings, output_file: str, prompt: str) -> list[str]:
    command = [
        settings.bin_path,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "--color",
        "never",
        "-C",
        str(PROJECT_ROOT),
        "-o",
        output_file,
    ]
    if settings.profile:
        command.extend(["-p", settings.profile])
    if settings.model:
        command.extend(["-m", settings.model])
    command.append(prompt)
    return command


def generate_json_object(
    *,
    system_prompt: str,
    user_prompt: str,
    workload: str = "deep",
) -> dict[str, Any]:
    del workload

    settings = resolve_llm_settings()
    if settings is None:
        return {}

    prompt = _compose_prompt(system_prompt, user_prompt)
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}".rstrip(":")

    with tempfile.NamedTemporaryFile(prefix="airotator-codex-", suffix=".txt", delete=False) as tmp:
        output_path = tmp.name

    command = _build_command(settings, output_path, prompt)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {}

    try:
        raw = Path(output_path).read_text().strip()
    finally:
        Path(output_path).unlink(missing_ok=True)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(f"codex exec exited {result.returncode}: {stderr or stdout}")

    if not raw:
        raw = (result.stdout or "").strip()
    return _extract_json(raw)
