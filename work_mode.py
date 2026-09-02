"""
JARVIS Work Mode — persistent coding-engine sessions tied to projects.

JARVIS can connect to any project directory and maintain a conversation with a
coding engine. OpenCode uses native continuation. Ollama sessions are rebuilt
from a rolling transcript managed by JARVIS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("jarvis.work_mode")

SESSION_FILE = Path(__file__).parent / "data" / "active_session.json"
DEFAULT_OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "openai/gpt-5.1-codex-mini")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b")


def available_coding_engines() -> dict[str, bool]:
    """Return installed coding engines."""
    return {
        "opencode": shutil.which("opencode") is not None,
        "ollama": shutil.which("ollama") is not None,
    }


def select_default_engine() -> str | None:
    """Pick the best installed engine."""
    available = available_coding_engines()
    if available["opencode"]:
        return "opencode"
    if available["ollama"]:
        return "ollama"
    return None


def build_task_brief(task_prompt: str) -> str:
    """Compile a neutral task brief for coding engines."""
    return (
        "# JARVIS Task\n\n"
        f"{task_prompt.strip()}\n\n"
        "## Expectations\n"
        "- Start building immediately unless the task is genuinely ambiguous.\n"
        "- Use the simplest reasonable implementation.\n"
        "- Write complete working files, not just a plan.\n"
        "- If a dev server is started, mention the localhost URL in the final output.\n"
    )


class CodingEngine:
    """Base class for coding-engine adapters."""

    name = "base"

    def is_available(self) -> bool:
        raise NotImplementedError

    async def send(self, user_text: str) -> str:
        raise NotImplementedError

    async def launch_terminal(self, project_dir: str, prompt: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        return {}

    def restore(self, data: dict):
        del data


class OpenCodeEngine(CodingEngine):
    name = "opencode"

    def __init__(self, working_dir: str):
        self.working_dir = working_dir
        self.session_id: str | None = None
        self.message_count = 0

    def is_available(self) -> bool:
        return available_coding_engines()["opencode"]

    async def send(self, user_text: str) -> str:
        opencode_path = shutil.which("opencode")
        if not opencode_path:
            return "OpenCode is not installed on this system."

        cmd = [
            opencode_path,
            "run",
            "--dir",
            self.working_dir,
            "--model",
            DEFAULT_OPENCODE_MODEL,
            "--format",
            "json",
            "--dangerously-skip-permissions",
            user_text,
        ]
        if self.session_id:
            cmd.extend(["--session", self.session_id])
        elif self.message_count > 0:
            cmd.append("--continue")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError:
            log.error("OpenCode timed out after 300s")
            return "That's taking longer than expected, sir. The operation timed out."
        except Exception as exc:
            log.error(f"OpenCode error: {exc}")
            return f"Something went wrong, sir: {str(exc)[:100]}"

        if process.returncode != 0:
            error = stderr.decode().strip()[:200]
            log.error(f"OpenCode error: {error}")
            return f"Hit a problem, sir: {error}"

        response = self._parse_output(stdout.decode())
        self.message_count += 1
        log.info(f"OpenCode response ({len(response)} chars)")
        return response

    def _parse_output(self, raw: str) -> str:
        text_chunks: list[str] = []
        lines = raw.splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                # If it's not JSON, it might be raw text output from the model
                text_chunks.append(stripped)
                continue

            if isinstance(payload, dict):
                # Try to capture session ID for continuation
                session_id = payload.get("sessionID") or payload.get("sessionId")
                if session_id:
                    self.session_id = session_id

                # OpenCode JSON can use various keys for the actual content
                val = (payload.get("message") or 
                       payload.get("content") or 
                       payload.get("delta") or 
                       payload.get("text") or
                       payload.get("response"))
                
                if isinstance(val, str):
                    text_chunks.append(val)
                elif isinstance(val, dict) and "content" in val:
                    text_chunks.append(val["content"])

        result = "\n".join(chunk for chunk in text_chunks if chunk).strip()
        if not result and raw.strip():
            log.debug(f"OpenCode raw output (no chunks parsed): {raw[:500]}...")
            # If we failed to parse anything but there WAS output, return the raw output as fallback
            return raw.strip()
        return result

    async def launch_terminal(self, project_dir: str, prompt: str) -> dict:
        from actions import launch_opencode

        return await launch_opencode(project_dir, prompt)

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "message_count": self.message_count,
        }

    def restore(self, data: dict):
        self.session_id = data.get("session_id")
        self.message_count = int(data.get("message_count", 0))


class OllamaEngine(CodingEngine):
    name = "ollama"

    def __init__(self, working_dir: str, project_name: str):
        self.working_dir = working_dir
        self.project_name = project_name
        self.history: list[dict[str, str]] = []

    def is_available(self) -> bool:
        return available_coding_engines()["ollama"]

    def _build_prompt(self, user_text: str) -> str:
        history_lines = []
        recent_history = self.history[-12:]
        for item in recent_history:
            history_lines.append(f"{item['role'].upper()}: {item['content']}")

        history_text = "\n".join(history_lines) if history_lines else "(start of session)"
        return (
            "You are JARVIS's coding engine working inside a local project.\n"
            "Be decisive. Write code, inspect files, and answer with concrete results.\n"
            "If a task is ambiguous, choose the simplest reasonable approach.\n\n"
            f"PROJECT: {self.project_name}\n"
            f"WORKING DIRECTORY: {self.working_dir}\n\n"
            f"RECENT SESSION:\n{history_text}\n\n"
            f"USER REQUEST:\n{user_text}"
        )

    async def send(self, user_text: str) -> str:
        ollama_path = shutil.which("ollama")
        if not ollama_path:
            return "Ollama is not installed on this system."

        prompt = self._build_prompt(user_text)
        cmd = [ollama_path, "run", DEFAULT_OLLAMA_MODEL, prompt]

        env = os.environ.copy()
        if env.get("OLLAMA_HOST"):
            env["OLLAMA_HOST"] = env["OLLAMA_HOST"]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError:
            log.error("Ollama timed out after 300s")
            return "That's taking longer than expected, sir. The operation timed out."
        except Exception as exc:
            log.error(f"Ollama error: {exc}")
            return f"Something went wrong, sir: {str(exc)[:100]}"

        if process.returncode != 0:
            error = stderr.decode().strip()[:200]
            log.error(f"Ollama error: {error}")
            return f"Hit a problem, sir: {error}"

        response = stdout.decode().strip()
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": response})
        log.info(f"Ollama response ({len(response)} chars)")
        return response

    async def launch_terminal(self, project_dir: str, prompt: str) -> dict:
        from actions import launch_ollama_workspace

        return await launch_ollama_workspace(project_dir, prompt)

    def snapshot(self) -> dict:
        return {"history": self.history[-20:]}

    def restore(self, data: dict):
        history = data.get("history", [])
        if isinstance(history, list):
            self.history = [item for item in history if isinstance(item, dict)]


def _make_engine(engine_name: str, working_dir: str, project_name: str) -> CodingEngine:
    if engine_name == "opencode":
        return OpenCodeEngine(working_dir)
    if engine_name == "ollama":
        return OllamaEngine(working_dir, project_name)
    raise ValueError(f"Unsupported coding engine: {engine_name}")


class WorkSession:
    """A coding-engine session tied to a project directory."""

    def __init__(self):
        self._active = False
        self._working_dir: str | None = None
        self._project_name: str | None = None
        self._status = "idle"
        self._engine_name: str | None = None
        self._engine: CodingEngine | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def project_name(self) -> str | None:
        return self._project_name

    @property
    def status(self) -> str:
        return self._status

    @property
    def engine_name(self) -> str | None:
        return self._engine_name

    async def start(self, working_dir: str, project_name: str | None = None, engine_name: str | None = None):
        """Start or switch to a project session."""
        self._working_dir = working_dir
        self._project_name = project_name or Path(working_dir).name
        self._engine_name = engine_name or select_default_engine()
        self._active = True
        self._status = "idle"

        if not self._engine_name:
            self._engine = None
            log.warning("No coding engine is installed")
        else:
            self._engine = _make_engine(self._engine_name, self._working_dir, self._project_name)

        self._save_session()
        log.info(f"Work mode started: {self._project_name} ({self._engine_name or 'none'})")

    async def send(self, user_text: str) -> str:
        """Send a message to the active coding engine."""
        if not self._engine:
            return "No coding engine is installed on this system."

        self._status = "working"
        response = await self._engine.send(user_text)
        self._status = "done"
        self._save_session()
        return response

    async def launch_terminal(self, prompt: str) -> dict:
        """Launch the active coding engine visibly in Terminal."""
        if not self._engine or not self._working_dir:
            return {"success": False, "confirmation": "No coding engine is available, sir."}
        return await self._engine.launch_terminal(self._working_dir, prompt)

    async def stop(self):
        """End the work session."""
        project = self._project_name
        self._active = False
        self._working_dir = None
        self._project_name = None
        self._status = "idle"
        self._engine_name = None
        self._engine = None
        self._clear_session()
        log.info(f"Work mode ended for {project}")

    def _save_session(self):
        """Persist session state so it survives restarts."""
        try:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps({
                "project_name": self._project_name,
                "working_dir": self._working_dir,
                "engine_name": self._engine_name,
                "engine_state": self._engine.snapshot() if self._engine else {},
            }))
        except Exception as exc:
            log.debug(f"Failed to save session: {exc}")

    def _clear_session(self):
        """Remove persisted session."""
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def restore(self) -> bool:
        """Restore session from disk after restart. Returns True if restored."""
        try:
            if not SESSION_FILE.exists():
                return False

            data = json.loads(SESSION_FILE.read_text())
            working_dir = data["working_dir"]
            project_name = data["project_name"]
            engine_name = data.get("engine_name") or select_default_engine()
            if not engine_name:
                return False

            await self.start(working_dir, project_name, engine_name=engine_name)
            if self._engine:
                self._engine.restore(data.get("engine_state", {}))
            self._status = "idle"
            log.info(f"Restored work session: {self._project_name} ({self._engine_name})")
            return True
        except Exception as exc:
            log.debug(f"No session to restore: {exc}")
        return False


def is_casual_question(text: str) -> bool:
    """Detect if a message is casual chat vs work-related."""
    t = text.lower().strip()

    casual_patterns = [
        "what time", "what's the time", "what day",
        "what's the weather", "weather",
        "how are you", "are you there", "hey jarvis",
        "good morning", "good evening", "good night",
        "thank you", "thanks", "never mind", "nevermind",
        "stop", "cancel", "quit work mode", "exit work mode",
        "go back to chat", "regular mode",
        "how's it going", "what's up",
        "are you still there", "you there", "jarvis",
        "are you doing it", "is it working", "what happened",
        "did you hear me", "hello", "hey",
        "how's that coming", "hows that coming",
        "any update", "status update",
    ]

    if len(t.split()) <= 3 and any(w in t for w in ["ok", "okay", "sure", "yes", "no", "yeah", "nah", "cool"]):
        return True

    return any(p in t for p in casual_patterns)
