from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server


def _fake_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


@pytest.mark.anyio
async def test_generate_response_routes_short_queries_to_fast_brain(monkeypatch):
    seen = {}

    async def fake_generate(*, messages, system, model=None, max_tokens=250):
        seen["messages"] = messages
        seen["system"] = system
        seen["model"] = model
        seen["max_tokens"] = max_tokens
        return _fake_response("Ready, sir.")

    monkeypatch.setattr(server.brain, "generate", fake_generate)
    monkeypatch.setattr(server.brain, "is_ready", lambda: True)

    result = await server.generate_response("hello jarvis", server.task_manager, [], [])
    assert result == "Ready, sir."
    assert seen["model"] == server.DEFAULT_CHAT_MODEL


@pytest.mark.anyio
async def test_generate_response_routes_complex_queries_to_reasoning_model(monkeypatch):
    seen = {}

    async def fake_generate(*, messages, system, model=None, max_tokens=250):
        seen["model"] = model
        return _fake_response("Here is the analysis.")

    monkeypatch.setattr(server.brain, "generate", fake_generate)
    monkeypatch.setattr(server.brain, "is_ready", lambda: True)

    result = await server.generate_response(
        "Please explain how the routing architecture should behave for a complex request",
        server.task_manager,
        [],
        [],
    )
    assert result == "Here is the analysis."
    assert seen["model"] in ("ollama/jarvis-qwen-think", server.PERSONALITY_MODEL, server.SOUL_MODEL if hasattr(server, 'SOUL_MODEL') else server.PERSONALITY_MODEL)


def test_settings_status_shape(monkeypatch):
    async def fake_events():
        return []

    async def fake_mail():
        return 0

    async def fake_notes(*args, **kwargs):
        assert kwargs.get("quiet") is True
        return []

    monkeypatch.setattr(server, "get_todays_events", fake_events)
    monkeypatch.setattr(server, "get_unread_count", fake_mail)
    monkeypatch.setattr(server, "get_recent_notes", fake_notes)
    monkeypatch.setattr(server, "get_important_memories", lambda limit=9999: [])
    monkeypatch.setattr(server, "get_open_tasks", lambda: [])
    monkeypatch.setattr(server, "_read_env", lambda: ("", {
        "GROQ_API_KEY": "groq-test",
        "GEMINI_API_KEY": "gemini-test",
        "NVIDIA_API_KEY": "",
        "USER_NAME": "Abrar",
    }))
    monkeypatch.setattr("server.available_coding_engines", lambda: {"opencode": True, "ollama": False})

    client = TestClient(server.app)
    response = client.get("/api/settings/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["coding_engines"] == {"opencode": True, "ollama": False}
    assert payload["env_keys_set"]["groq"] is True
    assert payload["env_keys_set"]["gemini"] is True
    assert "anthropic" not in payload["env_keys_set"]


def test_test_provider_endpoint_uses_litellm(monkeypatch):
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _fake_response("ok")

    monkeypatch.setattr(server.litellm, "acompletion", fake_acompletion)

    client = TestClient(server.app)
    response = client.post("/api/settings/test-provider", json={"provider": "groq", "key_value": "groq-test"})
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert calls[0]["model"] == server.PROVIDER_TEST_MODELS["groq"]
