"""
Classifier regression tests for the provider-agnostic intent classifier.
"""

from types import SimpleNamespace

import pytest

from server import apply_speech_corrections, classify_intent


TEST_CASES = [
    ("open the terminal", "open_terminal"),
    ("open cloud code", "open_terminal"),
    ("launch coding workspace", "open_terminal"),
    ("search for Python tutorials", "browse"),
    ("go to github.com", "browse"),
    ("build me a landing page", "build"),
    ("make a todo app with React", "build"),
    ("how are you doing today", "chat"),
]


def _fake_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


@pytest.mark.anyio
@pytest.mark.parametrize(("text", "expected"), TEST_CASES)
async def test_classify_intent_routes_expected_action(monkeypatch, text, expected):
    async def fake_generate(*args, **kwargs):
        del args, kwargs
        return _fake_response(f'{{"action": "{expected}", "target": ""}}')

    monkeypatch.setattr("server.brain.generate", fake_generate)

    corrected = apply_speech_corrections(text)
    result = await classify_intent(corrected)
    assert result["action"] == expected


@pytest.mark.anyio
async def test_classify_intent_falls_back_to_chat_on_bad_json(monkeypatch):
    async def fake_generate(*args, **kwargs):
        del args, kwargs
        return _fake_response("not-json")

    monkeypatch.setattr("server.brain.generate", fake_generate)
    result = await classify_intent("hello there")
    assert result["action"] == "chat"
