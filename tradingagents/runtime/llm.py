from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

from .paths import PROJECT_ROOT


FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class CodexSettings:
    bin_path: str
    model: str
    timeout_seconds: int
    profile: str


@dataclass(frozen=True)
class ApiSettings:
    provider: str
    model: str
    api_key: str
    base_url: str
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


def _api_key_for_provider(provider: str) -> str:
    env_names = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "glm": "GLM_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    return os.getenv(env_names.get(provider, ""), "").strip()


def _api_base_url(provider: str) -> str:
    overrides = {
        "deepseek": os.getenv("DEEPSEEK_BASE_URL", "").strip(),
        "openai": os.getenv("OPENAI_BASE_URL", "").strip(),
        "glm": os.getenv("GLM_BASE_URL", "").strip(),
        "anthropic": os.getenv("ANTHROPIC_BASE_URL", "").strip(),
    }
    if overrides.get(provider):
        return overrides[provider]
    defaults = {
        "deepseek": "https://api.deepseek.com/chat/completions",
        "openai": "https://api.openai.com/v1/chat/completions",
        "glm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "anthropic": "https://api.anthropic.com/v1/messages",
    }
    return defaults.get(provider, "")


def _profile_defaults(profile: str, workload: str) -> tuple[str, str]:
    path = PROJECT_ROOT / "config" / "llm_profiles.yaml"
    if not path.exists():
        return "", ""
    data = yaml.safe_load(path.read_text()) or {}
    profiles = data.get("profiles", {})
    selected = profiles.get(profile, {}) or {}
    suffix = "quick" if workload == "quick" else "deep"
    return str(selected.get(f"provider_{suffix}", "") or ""), str(selected.get(suffix, "") or "")


def _resolve_api_settings(workload: str) -> ApiSettings | None:
    profile = os.getenv("AI_ROTATOR_LLM_PROFILE", "").strip()
    profile_provider, profile_model = _profile_defaults(profile, workload) if profile else ("", "")
    provider = os.getenv("AI_ROTATOR_LLM_PROVIDER", profile_provider).strip().lower()
    model = os.getenv("AI_ROTATOR_LLM_MODEL", profile_model).strip()
    if not provider:
        return None
    key = _api_key_for_provider(provider)
    if not key:
        return None
    if not model:
        model = {
            "deepseek": "deepseek-chat",
            "openai": "gpt-5-mini",
            "glm": "glm-4.7",
            "anthropic": "claude-haiku-4-5",
        }.get(provider, "")
    base_url = _api_base_url(provider)
    if not base_url or not model:
        return None
    return ApiSettings(
        provider=provider,
        model=model,
        api_key=key,
        base_url=base_url,
        timeout_seconds=int(os.getenv("AI_ROTATOR_LLM_TIMEOUT", "180")),
        profile=profile,
    )


def resolve_llm_settings(workload: str = "deep") -> CodexSettings | ApiSettings | None:
    if not _is_enabled():
        return None

    api_settings = _resolve_api_settings(workload)
    if api_settings is not None:
        return api_settings

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


def _generate_via_api(settings: ApiSettings, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    if settings.provider == "anthropic":
        payload = {
            "model": settings.model,
            "max_tokens": 1200,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": settings.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        response = requests.post(settings.base_url, headers=headers, json=payload, timeout=settings.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        parts = data.get("content", [])
        raw = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        return _extract_json(raw)

    payload = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        settings.base_url,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=settings.timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _extract_json(raw)


def generate_json_object(
    *,
    system_prompt: str,
    user_prompt: str,
    workload: str = "deep",
) -> dict[str, Any]:
    settings = resolve_llm_settings(workload)
    if settings is None:
        return {}

    if isinstance(settings, ApiSettings):
        return _generate_via_api(settings, system_prompt=system_prompt, user_prompt=user_prompt)

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
