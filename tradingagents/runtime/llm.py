from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import certifi
import requests
import yaml

from .paths import PROJECT_ROOT


FALSE_VALUES = {"0", "false", "no", "off"}
DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int
    max_output_tokens: int
    profile: str


def _load_profiles() -> dict[str, Any]:
    path = PROJECT_ROOT / "config" / "llm_profiles.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("profiles", {}) if isinstance(data, dict) else {}


def _is_enabled() -> bool:
    raw = os.getenv("AI_ROTATOR_LLM_ENABLED", "1").strip().lower()
    return raw not in FALSE_VALUES


def _provider_env_key(provider: str) -> str:
    mapping = {
        "anthropic": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "glm": "GLM_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    return mapping.get(provider, "")


def _default_base_url(provider: str) -> str:
    defaults = {
        "anthropic": "https://api.anthropic.com",
        "deepseek": "https://api.deepseek.com",
        "glm": "https://open.bigmodel.cn/api/paas/v4",
        "openai": "https://api.openai.com/v1",
    }
    return defaults.get(provider, "")


def resolve_llm_settings(workload: str = "deep") -> LLMSettings | None:
    if not _is_enabled():
        return None

    profiles = _load_profiles()
    profile_name = os.getenv("AI_ROTATOR_LLM_PROFILE", DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
    profile = profiles.get(profile_name) or profiles.get(DEFAULT_PROFILE) or {}

    provider = (
        os.getenv("AI_ROTATOR_LLM_PROVIDER")
        or profile.get(f"provider_{workload}")
        or ""
    ).strip().lower()
    model = (
        os.getenv("AI_ROTATOR_LLM_MODEL")
        or profile.get(workload)
        or ""
    ).strip()
    if not provider or not model:
        return None

    api_key = (
        os.getenv("AI_ROTATOR_LLM_API_KEY")
        or os.getenv(_provider_env_key(provider))
        or ""
    ).strip()
    if not api_key:
        return None

    base_url = (
        os.getenv("AI_ROTATOR_LLM_BASE_URL")
        or os.getenv(f"{provider.upper()}_BASE_URL")
        or _default_base_url(provider)
    ).strip().rstrip("/")
    if not base_url:
        return None

    timeout_seconds = int(os.getenv("AI_ROTATOR_LLM_TIMEOUT", "90"))
    max_output_tokens = int(os.getenv("AI_ROTATOR_LLM_MAX_OUTPUT_TOKENS", "1200"))

    return LLMSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        profile=profile_name,
    )


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=timeout_seconds,
        verify=certifi.where(),
    )
    response.raise_for_status()
    return response.json()


def _extract_openai_output_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text", "")
                if text:
                    parts.append(str(text))
    return "".join(parts).strip()


def _call_openai(settings: LLMSettings, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": settings.model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "max_output_tokens": settings.max_output_tokens,
        "text": {"format": {"type": "json_object"}},
    }
    body = _post_json(
        f"{settings.base_url}/responses",
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout_seconds=settings.timeout_seconds,
    )
    return _extract_openai_output_text(body)


def _call_chat_completions(settings: LLMSettings, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": settings.max_output_tokens,
    }
    body = _post_json(
        f"{settings.base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout_seconds=settings.timeout_seconds,
    )
    return str(body["choices"][0]["message"]["content"]).strip()


def _call_anthropic(settings: LLMSettings, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": settings.model,
        "system": system_prompt,
        "max_tokens": settings.max_output_tokens,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    body = _post_json(
        f"{settings.base_url}/v1/messages",
        headers={
            "x-api-key": settings.api_key,
            "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout_seconds=settings.timeout_seconds,
    )
    content = body.get("content", [])
    parts = [block.get("text", "") for block in content if block.get("type") == "text"]
    return "".join(parts).strip()


def generate_json_object(
    *,
    system_prompt: str,
    user_prompt: str,
    workload: str = "deep",
) -> dict[str, Any]:
    settings = resolve_llm_settings(workload=workload)
    if settings is None:
        return {}

    if settings.provider == "openai":
        raw = _call_openai(settings, system_prompt, user_prompt)
    elif settings.provider in {"deepseek", "glm"}:
        raw = _call_chat_completions(settings, system_prompt, user_prompt)
    elif settings.provider == "anthropic":
        raw = _call_anthropic(settings, system_prompt, user_prompt)
    else:
        raise RuntimeError(f"Unsupported LLM provider: {settings.provider}")

    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
