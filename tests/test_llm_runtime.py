from __future__ import annotations

from unittest.mock import patch

from tradingagents.runtime.llm import generate_json_object, resolve_llm_settings


def test_resolve_llm_settings_from_profile():
    with patch.dict(
        "os.environ",
        {
            "AI_ROTATOR_LLM_PROFILE": "openai",
            "OPENAI_API_KEY": "sk-test",
        },
        clear=False,
    ):
        settings = resolve_llm_settings()
    assert settings is not None
    assert settings.provider == "openai"
    assert settings.model == "gpt-5.5"


def test_generate_json_object_openai_responses():
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"output_text": '{"ok": true, "provider": "openai"}'}

    with patch.dict(
        "os.environ",
        {
            "AI_ROTATOR_LLM_PROFILE": "openai",
            "OPENAI_API_KEY": "sk-test",
        },
        clear=False,
    ), patch("tradingagents.runtime.llm.requests.post", return_value=FakeResponse()) as post:
        payload = generate_json_object(system_prompt="system", user_prompt="user")

    assert payload == {"ok": True, "provider": "openai"}
    called_json = post.call_args.kwargs["json"]
    assert called_json["model"] == "gpt-5.5"
    assert called_json["text"]["format"]["type"] == "json_object"


def test_generate_json_object_anthropic_messages():
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"content": [{"type": "text", "text": '{"ok": true, "provider": "anthropic"}'}]}

    with patch.dict(
        "os.environ",
        {
            "AI_ROTATOR_LLM_PROVIDER": "anthropic",
            "AI_ROTATOR_LLM_MODEL": "claude-sonnet-4-5",
            "ANTHROPIC_API_KEY": "sk-ant",
        },
        clear=False,
    ), patch("tradingagents.runtime.llm.requests.post", return_value=FakeResponse()) as post:
        payload = generate_json_object(system_prompt="system", user_prompt="user")

    assert payload == {"ok": True, "provider": "anthropic"}
    assert post.call_args.kwargs["headers"]["x-api-key"] == "sk-ant"
