"""
JARVIS Action Executor — AppleScript-based system actions.

Execute actions immediately, before generating any LLM response.
Each function returns {"success": bool, "confirmation": str}.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger("jarvis.actions")

DESKTOP_PATH = Path.home() / "Desktop"


async def _mark_terminal_as_jarvis(revert_after: float = 5.0):
    """Temporarily set the front Terminal window to Ocean theme, then revert.

    Shows the user JARVIS is active in that terminal. Reverts after revert_after seconds.
    """
    # Save the current profile, switch to Ocean, then revert
    script_save = (
        'tell application "Terminal"\n'
        '    return name of current settings of front window\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_save,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        original_profile = stdout.decode().strip()

        # Switch to Ocean
        script_set = (
            'tell application "Terminal"\n'
            '    set current settings of front window to settings set "Ocean"\n'
            'end tell'
        )
        proc2 = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_set,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc2.communicate()

        # Schedule revert
        if original_profile and original_profile != "Ocean":
            asyncio.get_event_loop().call_later(
                revert_after,
                lambda: asyncio.ensure_future(_revert_terminal_theme(original_profile))
            )
    except Exception:
        pass


async def _revert_terminal_theme(profile_name: str):
    """Revert a Terminal window back to its original profile."""
    escaped = profile_name.replace('"', '\\"')
    script = (
        'tell application "Terminal"\n'
        f'    set current settings of front window to settings set "{escaped}"\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception:
        pass


async def open_terminal(command: str = "") -> dict:
    """Open Terminal.app and optionally run a command. Marks it blue for JARVIS."""
    if command:
        escaped = command.replace('"', '\\"')
        script = (
            'tell application "Terminal"\n'
            "    activate\n"
            f'    do script "{escaped}"\n'
            "end tell"
        )
    else:
        script = (
            'tell application "Terminal"\n'
            "    activate\n"
            "end tell"
        )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        log.error(f"open_terminal failed: {stderr.decode()}")
    else:
        await _mark_terminal_as_jarvis()
    return {
        "success": success,
        "confirmation": "Terminal is open, sir." if success else "I had trouble opening Terminal, sir.",
    }


def _write_task_file(project_dir: str, prompt: str) -> Path:
    """Persist the current task brief for visibility and debugging."""
    task_file = Path(project_dir) / ".jarvis_task.md"
    task_file.write_text(prompt)
    return task_file


def _escape_shell(value: str) -> str:
    return shlex.quote(value)


def _terminal_script(project_dir: str, command: str) -> str:
    return (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {_escape_shell(project_dir)} && {command}"\n'
        "end tell"
    )


BROWSER_APPS = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "brave": "Brave Browser",
    "brave browser": "Brave Browser",
    "safari": "Safari",
    "firefox": "Firefox",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "arc": "Arc",
}


async def open_browser(url: str, browser: str = "chrome") -> dict:
    """Open URL in user's browser (Chrome, Brave, Safari, Firefox, Edge, Arc)."""
    b_key = browser.lower().strip()
    app_name = BROWSER_APPS.get(b_key, "Google Chrome")
    escaped_url = url.replace('"', '\\"')

    # Handle local file paths
    file_path = None
    if url.startswith("file://"):
        file_path = url[7:]
    elif os.path.exists(url):
        file_path = url

    if file_path and os.path.exists(file_path):
        # Open local HTML or file in specific browser or default
        try:
            p = await asyncio.create_subprocess_exec(
                "open", "-a", app_name, file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await p.communicate()
            if p.returncode == 0:
                display_name = app_name.replace(" Browser", "")
                return {"success": True, "confirmation": f"Pulled that up in {display_name}, sir."}
        except Exception:
            pass
        # Fallback to system default open
        try:
            p_def = await asyncio.create_subprocess_exec("open", file_path)
            await p_def.communicate()
            return {"success": True, "confirmation": "Opened report in browser, sir."}
        except Exception:
            pass

    script = (
        f'tell application "{app_name}"\n'
        "    activate\n"
        f'    open location "{escaped_url}"\n'
        "end tell"
    )

    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        # Fallback to open -a
        try:
            p2 = await asyncio.create_subprocess_exec(
                "open", "-a", app_name, url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await p2.communicate()
            success = p2.returncode == 0
        except Exception:
            pass

    display_name = app_name.replace(" Browser", "")
    return {
        "success": success,
        "confirmation": f"Pulled that up in {display_name}, sir." if success else f"{display_name} ran into a problem, sir.",
    }


async def open_maps(destination: str, browser: str = "chrome") -> dict:
    """Open Maps route planning / directions with live ETA, distance, and traffic analysis."""
    raw = (destination or "").strip()

    # Extract browser preference if embedded
    browser_clean = browser
    for b in ("brave", "safari", "firefox", "edge", "arc", "chrome", "apple"):
        if f" in {b}" in raw.lower() or f" on {b}" in raw.lower():
            browser_clean = b
            raw = re.sub(rf"\s+(?:in|on)\s+{b}(?:\s+browser)?", "", raw, flags=re.I).strip()
            break

    # Strip conversational noise from destination string
    dest_clean = raw
    dest_clean = re.sub(r'^(?:okay\s+|so\s+|now\s+|please\s+|and\s+|can\s+you\s+)+', '', dest_clean, flags=re.I).strip()
    dest_clean = re.sub(r'^(?:plan\s+(?:my\s+|a\s+)?trip|directions|route|map|navigate)\s+(?:for\s+me\s+)?(?:to\s+|for\s+)?', '', dest_clean, flags=re.I).strip()
    dest_clean = re.sub(r'^(?:to\s+|for\s+|towards\s+)', '', dest_clean, flags=re.I).strip()
    dest_clean = re.sub(r'\s+in\s+(?:google\s+|apple\s+)?maps?.*$', '', dest_clean, flags=re.I).strip()
    dest_clean = re.sub(r'\s+(?:in|on)\s+(?:chrome|brave|safari|firefox|edge|arc).*$', '', dest_clean, flags=re.I).strip()

    # Check for "from X to Y" or "X to Y" format
    origin = ""
    dest = dest_clean
    from_match = re.match(r'^(?:from\s+)?(.+?)\s+(?:to|towards)\s+(.+)$', dest_clean, flags=re.I)
    if from_match:
        origin = from_match.group(1).strip()
        dest = from_match.group(2).strip()

    if not dest or dest.lower() in ("maps", "google maps", "apple maps", "that", "there", "it"):
        dest = "Ahmedabad"

    # Calculate real-time route info
    from browser_vision import calculate_route_realtime
    route_info = await calculate_route_realtime(origin, dest)

    # Apple Maps handling
    if "apple" in browser_clean.lower():
        apple_url = f"https://maps.apple.com/?daddr={quote(dest)}"
        if origin:
            apple_url += f"&saddr={quote(origin)}"
        try:
            p = await asyncio.create_subprocess_exec("open", "-a", "Maps", apple_url)
            await p.communicate()
            return {"success": True, "confirmation": route_info.voice_summary}
        except Exception:
            pass

    # Default to Google Maps in requested browser
    if origin:
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&origin={quote(origin)}&destination={quote(dest)}&travelmode=driving"
    else:
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={quote(dest)}&travelmode=driving"

    await open_browser(gmaps_url, browser_clean)
    return {
        "success": True,
        "confirmation": route_info.voice_summary,
        "route_info": {
            "origin": route_info.origin,
            "destination": route_info.destination,
            "duration": route_info.duration_text,
            "distance": route_info.distance_text,
            "road": route_info.summary_road,
        }
    }


# Keep backward compat
async def open_chrome(url: str) -> dict:
    return await open_browser(url, "chrome")


async def open_claude_in_project(project_dir: str, prompt: str) -> dict:
    """Backward-compatible wrapper for opening a coding workspace."""
    if shutil.which("opencode"):
        return await launch_opencode(project_dir, prompt)
    if shutil.which("ollama"):
        return await launch_ollama_workspace(project_dir, prompt)
    return {
        "success": False,
        "confirmation": "No coding workspace engine is installed, sir.",
    }


async def launch_opencode(project_dir: str, prompt: str) -> dict:
    """Launch OpenCode in a project directory."""
    opencode_cmd = shutil.which("opencode") or os.getenv("OPENCODE_CMD", "opencode")
    model = os.getenv("OPENCODE_MODEL", "openai/gpt-5.1-codex-mini")
    task_file = _write_task_file(project_dir, prompt)
    command = (
        f"{_escape_shell(opencode_cmd)} {_escape_shell(project_dir)} "
        f"--model {_escape_shell(model)} "
        f"--prompt {_escape_shell(task_file.read_text())}"
    )
    script = _terminal_script(project_dir, command)
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        log.error(f"launch_opencode failed: {stderr.decode()}")
    else:
        await _mark_terminal_as_jarvis()
    return {
        "success": success,
        "confirmation": "OpenCode is running in Terminal, sir."
        if success
        else "Had trouble starting OpenCode, sir.",
    }


async def launch_ollama_workspace(project_dir: str, prompt: str) -> dict:
    """Launch an Ollama coding workspace in Terminal."""
    ollama_cmd = shutil.which("ollama") or "ollama"
    ollama_host = os.getenv("OLLAMA_HOST", "").strip()
    model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
    compiled_prompt = _write_task_file(project_dir, prompt).read_text()
    env_prefix = f"OLLAMA_HOST={_escape_shell(ollama_host)} " if ollama_host else ""
    command = f"{env_prefix}{_escape_shell(ollama_cmd)} run {_escape_shell(model)} {_escape_shell(compiled_prompt)}"
    script = _terminal_script(project_dir, command)
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    success = proc.returncode == 0
    if not success:
        log.error(f"launch_ollama_workspace failed: {stderr.decode()[:200]}")
    else:
        await _mark_terminal_as_jarvis()
    return {
        "success": success,
        "confirmation": "Ollama is running in Terminal, sir." if success else "Had trouble starting Ollama, sir.",
    }


async def launch_ollama_cloud(project_dir: str, prompt: str) -> dict:
    """Backward-compatible alias for Ollama workspace launch."""
    return await launch_ollama_workspace(project_dir, prompt)


async def prompt_existing_terminal(project_name: str, prompt: str) -> dict:
    """Find a Terminal window matching a project name and type a prompt into it.

    Uses System Events keystroke to type into an active coding workspace
    rather than `do script` which would open a new shell.
    """
    escaped_name = project_name.replace('"', '\\"')
    escaped_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"')

    # Single atomic script: find window, focus it, type into it
    script = f'''
tell application "Terminal"
    set matched to false
    set targetWindow to missing value
    repeat with w in windows
        if name of w contains "{escaped_name}" then
            set targetWindow to w
            set matched to true
            exit repeat
        end if
    end repeat

    if not matched then
        return "NOT_FOUND"
    end if

    -- Bring the matched window to front
    set index of targetWindow to 1
    set selected tab of targetWindow to selected tab of targetWindow
    activate
end tell

-- Wait for window to be fully focused
delay 1

-- Now type into it
tell application "System Events"
    tell process "Terminal"
        set frontmost to true
        delay 0.3
        keystroke "{escaped_prompt}"
        delay 0.2
        keystroke return
    end tell
end tell

return "OK"
'''

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

        result = stdout.decode().strip()
        if result == "NOT_FOUND":
            return {
                "success": False,
                "confirmation": f"Couldn't find a terminal for {project_name}, sir.",
            }

        success = proc.returncode == 0
        if not success:
            log.error(f"prompt_existing_terminal failed: {stderr.decode()[:200]}")

        if success:
            await _mark_terminal_as_jarvis()

        return {
            "success": success,
            "confirmation": f"Sent that to {project_name}, sir." if success
            else f"Had trouble typing into {project_name}, sir.",
        }

    except asyncio.TimeoutError:
        return {"success": False, "confirmation": "Terminal operation timed out, sir."}
    except Exception as e:
        log.error(f"prompt_existing_terminal failed: {e}")
        return {"success": False, "confirmation": "Something went wrong reaching that terminal, sir."}


async def get_chrome_tab_info() -> dict:
    """Read the current Chrome tab's title and URL via AppleScript."""
    script = (
        'tell application "Google Chrome"\n'
        "    set tabTitle to title of active tab of front window\n"
        "    set tabURL to URL of active tab of front window\n"
        '    return tabTitle & "|" & tabURL\n'
        "end tell"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            result = stdout.decode().strip()
            parts = result.split("|", 1)
            if len(parts) == 2:
                return {"title": parts[0], "url": parts[1]}
        return {}
    except Exception as e:
        log.warning(f"get_chrome_tab_info failed: {e}")
        return {}


async def monitor_build(project_dir: str, ws=None, synthesize_fn=None) -> None:
    """Monitor a build for completion. Notify via WebSocket when done."""
    import base64

    output_file = Path(project_dir) / ".jarvis_output.txt"
    start = time.time()
    timeout = 600  # 10 minutes

    while time.time() - start < timeout:
        await asyncio.sleep(5)
        if output_file.exists():
            content = output_file.read_text()
            if "--- JARVIS TASK COMPLETE ---" in content:
                log.info(f"Build complete in {project_dir}")
                if ws and synthesize_fn:
                    try:
                        msg = "The build is complete, sir."
                        audio_bytes = await synthesize_fn(msg)
                        if audio_bytes:
                            encoded = base64.b64encode(audio_bytes).decode()
                            await ws.send_json({"type": "status", "state": "speaking"})
                            await ws.send_json({"type": "audio", "data": encoded, "text": msg})
                            await ws.send_json({"type": "status", "state": "idle"})
                    except Exception as e:
                        log.warning(f"Build notification failed: {e}")
                return

    log.warning(f"Build timed out in {project_dir}")


APP_ALIASES = {
    "spotify": "Spotify",
    "whatsapp": "WhatsApp",
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "safari": "Safari",
    "brave": "Brave Browser",
    "brave browser": "Brave Browser",
    "firefox": "Firefox",
    "antigravity": "Antigravity",
    "vscode": "Visual Studio Code",
    "code": "Visual Studio Code",
    "cursor": "Cursor",
    "terminal": "Terminal",
    "iterm": "iTerm",
    "calendar": "Calendar",
    "notes": "Notes",
    "mail": "Mail",
    "email": "Mail",
    "messages": "Messages",
    "imessage": "Messages",
    "music": "Music",
    "apple music": "Music",
    "calculator": "Calculator",
    "finder": "Finder",
    "slack": "Slack",
    "discord": "Discord",
    "notion": "Notion",
    "telegram": "Telegram",
}

WEB_SERVICES = {
    "google meet": "https://meet.google.com",
    "meet": "https://meet.google.com",
    "google meeting": "https://meet.google.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "twitter": "https://x.com",
    "reddit": "https://reddit.com",
}


async def open_macos_app(app_name: str) -> dict:
    """Launch or focus any macOS application by name with fuzzy matching."""
    raw = (app_name or "").strip().lower()
    raw = re.sub(r"^(the\s+|open\s+|launch\s+|start\s+)?", "", raw).strip()
    raw = re.sub(r"\s+app(lication)?$", "", raw).strip()

    # Check for browser specification (e.g. "google meet on browser", "google meet in brave")
    target_browser = "chrome"
    for b_name in ("brave", "safari", "firefox", "edge", "arc", "chrome", "google chrome"):
        if f" on {b_name}" in raw or f" in {b_name}" in raw:
            target_browser = b_name.replace("google chrome", "chrome")
            raw = re.sub(rf"\s+(?:on|in)\s+{b_name}$", "", raw).strip()
            break
    if " on browser" in raw or " in browser" in raw or " on the browser" in raw or " in the browser" in raw:
        raw = re.sub(r"\s+(?:on|in)\s+(?:the\s+)?browser$", "", raw).strip()

    # Check if this is a web service like Google Meet
    for svc_name, svc_url in WEB_SERVICES.items():
        if raw == svc_name or raw == f"{svc_name} on browser" or raw == f"{svc_name} in browser":
            return await open_browser(svc_url, target_browser)

    resolved_name = APP_ALIASES.get(raw, app_name.strip())

    # Try open -a
    try:
        proc = await asyncio.create_subprocess_exec(
            "open", "-a", resolved_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return {"success": True, "confirmation": f"Opening {resolved_name}, sir."}
    except Exception:
        pass

    # AppleScript activate fallback
    escaped = resolved_name.replace('"', '\\"')
    script = f'tell application "{escaped}" to activate'
    try:
        proc2 = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc2.communicate()
        if proc2.returncode == 0:
            return {"success": True, "confirmation": f"Opening {resolved_name}, sir."}
    except Exception:
        pass

    return {"success": False, "confirmation": f"I couldn't locate {app_name} on your system, sir."}


async def close_macos_app(app_name: str) -> dict:
    """Gracefully quit one or more macOS applications by name."""
    raw = (app_name or "").strip().lower()
    raw = re.sub(r"^(the\s+|close\s+|quit\s+|kill\s+|exit\s+|shut\s+down\s+)?", "", raw).strip()
    raw = re.sub(r"\s+app(lication)?s?$", "", raw).strip()

    # Split multi-app lists (e.g. "email notes and calendar", "safari, chrome, spotify")
    norm = re.sub(r'[,&]|\band\b', ' ', raw)
    tokens = norm.split()
    apps = []
    i = 0
    while i < len(tokens):
        if i + 1 < len(tokens):
            two_word = f"{tokens[i]} {tokens[i+1]}".lower()
            if two_word in APP_ALIASES:
                apps.append(APP_ALIASES[two_word])
                i += 2
                continue
        one_word = tokens[i].lower()
        if one_word in APP_ALIASES:
            apps.append(APP_ALIASES[one_word])
        elif len(one_word) > 2 and one_word not in ("the", "app", "apps", "and", "all"):
            apps.append(one_word.title())
        i += 1

    # Remove duplicates preserving order
    apps = list(dict.fromkeys(apps))
    if not apps:
        apps = [app_name.strip().title()]

    closed_names = []
    not_running_names = []

    for resolved_name in apps:
        script = f'''
        tell application "System Events"
            if exists (processes where name is "{resolved_name}") then
                tell application "{resolved_name}" to quit
                return "ok"
            else
                return "not_running"
            end if
        end tell
        '''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            res = stdout.decode().strip()
            if res == "ok":
                closed_names.append(resolved_name)
            else:
                # Also try killall as fallback if process name matches
                try:
                    p2 = await asyncio.create_subprocess_exec(
                        "killall", resolved_name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await p2.communicate()
                    if p2.returncode == 0:
                        closed_names.append(resolved_name)
                    else:
                        not_running_names.append(resolved_name)
                except Exception:
                    not_running_names.append(resolved_name)
        except Exception as e:
            log.warning(f"close_macos_app error for {resolved_name}: {e}")

    if closed_names:
        return {"success": True, "confirmation": f"Closed {', '.join(closed_names)}, sir."}
    elif not_running_names:
        return {"success": True, "confirmation": f"{', '.join(not_running_names)} isn't running, sir."}

    return {"success": False, "confirmation": f"Couldn't close {app_name}, sir."}


async def control_spotify(command: str = "play", query: str = "") -> dict:
    """Control Spotify (play, pause, resume, next, previous, search & play)."""
    cmd = (command or "play").strip().lower()
    q = (query or "").strip()

    # Normalization: If query is actually a control verb or generic word, convert to command
    if q.lower() in ("pause", "stop"):
        cmd = "pause"
        q = ""
    elif q.lower() in ("resume", "unpause", "continue"):
        cmd = "resume"
        q = ""
    elif q.lower() in ("next", "skip"):
        cmd = "next"
        q = ""
    elif q.lower() in ("previous", "prev"):
        cmd = "previous"
        q = ""
    elif q.lower() in ("play", "some music", "music", "songs", "playlist", "the music", "the song", "song", "a song"):
        cmd = "play"
        q = ""

    # Pause / Stop: do NOT open/focus app or search, just pause if running
    if "pause" in cmd or "stop" in cmd:
        script = '''
        tell application "System Events"
            if exists (processes where name is "Spotify") then
                tell application "Spotify" to pause
                return "ok"
            else
                return "not_running"
            end if
        end tell
        '''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            res = stdout.decode().strip()
            if res == "not_running":
                return {"success": True, "confirmation": "Spotify isn't running, sir."}
            return {"success": True, "confirmation": "Paused Spotify playback, sir."}
        except Exception as e:
            return {"success": False, "confirmation": f"Couldn't pause Spotify: {e}"}

    # Resume / Unpause
    if "resume" in cmd or "unpause" in cmd:
        try:
            script = 'tell application "Spotify" to play'
            await asyncio.create_subprocess_exec("osascript", "-e", script)
            return {"success": True, "confirmation": "Resumed Spotify playback, sir."}
        except Exception as e:
            return {"success": False, "confirmation": f"Couldn't resume Spotify: {e}"}

    # Track navigation
    if "next" in cmd or "skip" in cmd:
        try:
            script = 'tell application "Spotify" to next track'
            await asyncio.create_subprocess_exec("osascript", "-e", script)
            return {"success": True, "confirmation": "Skipping to the next track, sir."}
        except Exception as e:
            return {"success": False, "confirmation": f"Couldn't skip track: {e}"}

    if "previous" in cmd or "prev" in cmd:
        try:
            script = 'tell application "Spotify" to previous track'
            await asyncio.create_subprocess_exec("osascript", "-e", script)
            return {"success": True, "confirmation": "Playing previous track, sir."}
        except Exception as e:
            return {"success": False, "confirmation": f"Couldn't change track: {e}"}

    # Clean natural language filler phrases from query
    if q:
        q_cleaned = re.sub(r'\b(that\s+i\s+can\s+(?:listen|work|study|focus)\s+(?:with|to)?(?:.*)?)\b', '', q, flags=re.I).strip()
        q_cleaned = re.sub(r'\b(to\s+(?:work|study|focus|listen|relax)\s+(?:with|to)?(?:.*)?)\b', '', q_cleaned, flags=re.I).strip()
        q_cleaned = re.sub(r'^(something\s+|some\s+|a\s+|good\s+|nice\s+)', '', q_cleaned, flags=re.I).strip()
        if q_cleaned:
            q = q_cleaned

    # Play / Search: Ensure Spotify is running
    await open_macos_app("Spotify")
    await asyncio.sleep(0.3)

    if q and q.lower() not in ("play", "music", "some music", "the music", "songs", "song", "playlist"):
        escaped_q = quote(q)
        try:
            proc = await asyncio.create_subprocess_exec("open", f"spotify:search:{escaped_q}")
            await proc.communicate()
            await asyncio.sleep(0.8)
            script_play = 'tell application "Spotify" to play'
            await asyncio.create_subprocess_exec("osascript", "-e", script_play)
            return {"success": True, "confirmation": f"Playing {q} on Spotify, sir."}
        except Exception as e:
            log.warning(f"Spotify search error: {e}")

    try:
        script = 'tell application "Spotify" to play'
        await asyncio.create_subprocess_exec("osascript", "-e", script)
        return {"success": True, "confirmation": "Playing music on Spotify, sir."}
    except Exception as e:
        return {"success": False, "confirmation": f"Couldn't play Spotify: {e}"}


async def open_whatsapp(contact: str = "", message: str = "") -> dict:
    """Open WhatsApp Desktop and optionally initiate chat with contact."""
    contact_clean = (contact or "").strip()
    msg_clean = (message or "").strip()

    await open_macos_app("WhatsApp")

    if contact_clean:
        digits = re.sub(r"[^\d+]", "", contact_clean)
        if len(digits) >= 10:
            url = f"whatsapp://send?phone={quote(digits)}"
            if msg_clean:
                url += f"&text={quote(msg_clean)}"
            try:
                await asyncio.create_subprocess_exec("open", url)
                return {"success": True, "confirmation": f"Opening WhatsApp chat with {contact_clean}, sir."}
            except Exception:
                pass

        # Keystroke search in WhatsApp Desktop
        script = f'''
        tell application "System Events"
            tell process "WhatsApp"
                set frontmost to true
                delay 0.3
                keystroke "f" using command down
                delay 0.2
                keystroke "{contact_clean}"
                delay 0.5
                key code 36
            end tell
        end tell
        '''
        try:
            await asyncio.create_subprocess_exec("osascript", "-e", script)
            return {"success": True, "confirmation": f"Opening chat with {contact_clean} on WhatsApp, sir."}
        except Exception as e:
            log.warning(f"WhatsApp search error: {e}")

    return {"success": True, "confirmation": "WhatsApp is open, sir."}


async def open_local_folder(folder_name: str) -> dict:
    """Open a folder from Desktop or Projects in Finder."""
    name_clean = folder_name.strip().strip('"').strip("'")
    if not name_clean:
        path = str(DESKTOP_PATH)
    else:
        # Search Desktop
        desktop_target = DESKTOP_PATH / name_clean
        if desktop_target.exists():
            path = str(desktop_target)
        else:
            # Fuzzy match on Desktop
            matches = [p for p in DESKTOP_PATH.iterdir() if p.is_dir() and name_clean.lower() in p.name.lower()]
            if matches:
                path = str(matches[0])
            else:
                path = str(DESKTOP_PATH)

    try:
        proc = await asyncio.create_subprocess_exec("open", path)
        await proc.communicate()
        return {"success": True, "confirmation": f"Opening {Path(path).name} in Finder, sir."}
    except Exception as e:
        return {"success": False, "confirmation": f"Couldn't open folder: {e}"}


async def firecrawl_scrape(url: str, prompt: str = "") -> dict:
    """Scrape web content using Firecrawl if API key is present."""
    api_key = os.getenv("FIRECRAWL_API_KEY", "")
    if not api_key:
        return {"success": False, "confirmation": "Firecrawl API key is not configured, sir."}

    clean_url = url.strip()
    if not clean_url.startswith("http"):
        clean_url = f"https://{clean_url}"

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"url": clean_url, "formats": ["markdown"]}
            if prompt:
                payload["extract"] = {"prompt": prompt}
            async with session.post(
                "https://api.firecrawl.dev/v1/scrape",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markdown = data.get("data", {}).get("markdown", "")
                    title = data.get("data", {}).get("metadata", {}).get("title", clean_url)
                    summary = markdown[:300].strip() if markdown else "Content retrieved."
                    return {"success": True, "confirmation": f"Retrieved {title}: {summary}"}
                else:
                    return {"success": False, "confirmation": f"Firecrawl returned status {resp.status}, sir."}
    except Exception as e:
        return {"success": False, "confirmation": f"Firecrawl scrape failed: {e}"}


async def execute_action(intent: dict, projects: list = None) -> dict:
    """Route a classified intent to the right action function."""
    action = intent.get("action", "chat")
    target = intent.get("target", "")

    if action == "open_app":
        result = await open_macos_app(target)
        result["project_dir"] = None
        return result

    elif action == "close_app":
        result = await close_macos_app(target)
        result["project_dir"] = None
        return result

    elif action == "spotify":
        # target might be "play ||| song", "pause", "resume", etc.
        t_clean = (target or "").strip()
        if "|||" in t_clean:
            cmd, _, q = t_clean.partition("|||")
            result = await control_spotify(cmd.strip(), q.strip())
        elif t_clean.lower() in ("pause", "stop", "resume", "unpause", "next", "skip", "previous", "prev"):
            result = await control_spotify(t_clean, "")
        else:
            result = await control_spotify("play", t_clean)
        result["project_dir"] = None
        return result

    elif action == "whatsapp":
        if "|||" in target:
            contact, _, msg = target.partition("|||")
            result = await open_whatsapp(contact.strip(), msg.strip())
        else:
            result = await open_whatsapp(target.strip())
        result["project_dir"] = None
        return result

    elif action in ("maps", "open_maps", "directions"):
        browser = "chrome"
        target_clean = target
        for b in ("brave", "safari", "firefox", "edge", "arc", "apple"):
            if f" in {b}" in target.lower() or f" on {b}" in target.lower():
                browser = b
                target_clean = re.sub(rf"\s+(?:in|on)\s+{b}(?:\s+browser)?", "", target_clean, flags=re.I).strip()
                break
        result = await open_maps(target_clean, browser)
        result["project_dir"] = None
        return result

    elif action == "open_folder":
        result = await open_local_folder(target)
        result["project_dir"] = None
        return result

    elif action in ("schedule", "schedule_event", "calendar_schedule"):
        from calendar_access import create_calendar_event
        if "|||" in target:
            title, _, time_str = target.partition("|||")
            result = await create_calendar_event(title.strip(), time_str.strip())
        else:
            result = await create_calendar_event(target.strip())
        result["project_dir"] = None
        return result

    elif action == "firecrawl":
        if "|||" in target:
            url, _, prompt = target.partition("|||")
            result = await firecrawl_scrape(url.strip(), prompt.strip())
        else:
            result = await firecrawl_scrape(target.strip())
        result["project_dir"] = None
        return result

    elif action == "open_terminal":
        if shutil.which("opencode"):
            result = await open_terminal("opencode .")
        elif shutil.which("ollama"):
            model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
            result = await open_terminal(f"ollama run {model}")
        else:
            result = {"success": False, "confirmation": "No coding workspace engine is installed, sir."}
        result["project_dir"] = None
        return result

    elif action == "flights":
        from flights import search_flights
        orig = "Himmatnagar, Gujarat"
        dest = target
        if " from " in target.lower():
            parts = target.lower().split(" from ")
            dest = parts[0].replace("to ", "").strip()
            orig = parts[1].strip()
        elif " to " in target.lower():
            parts = target.lower().split(" to ")
            orig = parts[0].replace("from ", "").strip() or orig
            dest = parts[1].strip()
        f_res = await search_flights(orig, dest)
        return {
            "success": True,
            "confirmation": f_res["speech"],
            "markdown_card": f_res.get("markdown_card", ""),
            "flights": f_res.get("flights", []),
            "flights_url": f_res.get("flights_url", ""),
            "project_dir": None,
        }

    elif action == "browse":
        target_clean = target.strip().strip('"').strip("'")
        target_lower = target_clean.lower()

        # Check browser specified
        browser = "chrome"
        if " in brave" in target_lower or " on brave" in target_lower:
            browser = "brave"
            target_clean = re.sub(r"\s+(in|on)\s+brave$", "", target_clean, flags=re.IGNORECASE).strip()
        elif " in safari" in target_lower or " on safari" in target_lower:
            browser = "safari"
            target_clean = re.sub(r"\s+(in|on)\s+safari$", "", target_clean, flags=re.IGNORECASE).strip()
        elif " in firefox" in target_lower or " on firefox" in target_lower:
            browser = "firefox"
            target_clean = re.sub(r"\s+(in|on)\s+firefox$", "", target_clean, flags=re.IGNORECASE).strip()
        elif "brave" in target_lower:
            browser = "brave"
        elif "safari" in target_lower:
            browser = "safari"
        elif "firefox" in target_lower:
            browser = "firefox"

        # Check if query is a web service like "Google Meet"
        matched_url = None
        for svc_k, svc_v in WEB_SERVICES.items():
            if target_clean.lower() == svc_k or target_clean.lower() == f"open {svc_k}":
                matched_url = svc_v
                break

        display_subject = target_clean
        if matched_url:
            url = matched_url
            display_subject = target_clean.title()
        elif target_clean.startswith("http://") or target_clean.startswith("https://"):
            url = target_clean
            # Extract clean search query if it is a google search URL
            if "google.com/search" in target_clean:
                parsed = urllib.parse.urlparse(target_clean)
                qs = urllib.parse.parse_qs(parsed.query)
                display_subject = qs.get("q", [target_clean])[0]
            else:
                display_subject = urllib.parse.urlparse(target_clean).netloc
        else:
            url = f"https://www.google.com/search?q={quote(target_clean)}"
            display_subject = target_clean

        result = await open_browser(url, browser)
        if matched_url:
            result["confirmation"] = f"Opening {display_subject} in {browser.title()}, sir."
        else:
            result["confirmation"] = f"Searching for {display_subject} in {browser.title()}, sir."
        result["project_dir"] = None
        return result

    elif action == "build":
        project_name = _generate_project_name(target)
        project_dir = str(DESKTOP_PATH / project_name)
        os.makedirs(project_dir, exist_ok=True)
        result = await open_claude_in_project(project_dir, target)
        result["project_dir"] = project_dir
        return result

    else:
        return {"success": False, "confirmation": "", "project_dir": None}


def _generate_project_name(prompt: str) -> str:
    """Generate a kebab-case project folder name from the prompt."""
    # First: check for a quoted name like "tiktok-analytics-dashboard"
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = quoted.group(1).strip()
        # Already kebab-case or close to it
        name = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip()
        if name:
            return re.sub(r"[\s]+", "-", name.lower())

    # Second: check for "called X" or "named X" pattern
    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()

    # Fallback: extract meaningful words
    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
    skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
            "to", "of", "i", "want", "need", "new", "project", "directory", "called",
            "on", "desktop", "that", "application", "app", "full", "stack", "simple",
            "web", "page", "site", "named"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    return "-".join(meaningful) if meaningful else "jarvis-project"
