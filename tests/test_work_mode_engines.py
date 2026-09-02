from types import SimpleNamespace

import pytest

from work_mode import OllamaEngine, OpenCodeEngine, WorkSession


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input=None):
        del input
        return self._stdout, self._stderr


@pytest.mark.anyio
async def test_opencode_engine_uses_continue_and_session(monkeypatch):
    commands = []

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        del kwargs
        commands.append(cmd)
        payload = b'{"sessionID":"sess-123","message":"First response"}\n'
        return FakeProcess(stdout=payload)

    monkeypatch.setattr("work_mode.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("work_mode.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    engine = OpenCodeEngine("/tmp/project")
    first = await engine.send("hello")
    second = await engine.send("follow up")

    assert first == "First response"
    assert second == "First response"
    assert "--session" in commands[1]
    assert "sess-123" in commands[1]


@pytest.mark.anyio
async def test_ollama_engine_replays_recent_history(monkeypatch):
    commands = []

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        commands.append(cmd)
        return FakeProcess(stdout=b"Done.")

    monkeypatch.setattr("work_mode.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("work_mode.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    engine = OllamaEngine("/tmp/project", "project")
    await engine.send("first task")
    await engine.send("second task")

    compiled_prompt = commands[-1][-1]
    assert "USER REQUEST:\nsecond task" in compiled_prompt
    assert "USER: first task" in compiled_prompt
    assert "ASSISTANT: Done." in compiled_prompt


@pytest.mark.anyio
async def test_work_session_selects_requested_engine(monkeypatch):
    monkeypatch.setattr("work_mode.available_coding_engines", lambda: {"opencode": True, "ollama": True})
    session = WorkSession()
    await session.start("/tmp/project", "project", engine_name="ollama")
    assert session.engine_name == "ollama"
