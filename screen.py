"""
JARVIS Screen Awareness — see what's on the user's screen.

Two capabilities:
1. Window/app list via AppleScript (fast, text-based)
2. Screenshot via screencapture → Claude vision API (sees everything)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("jarvis.screen")


async def get_active_windows() -> list[dict]:
    """Get list of visible windows with app name, window title, and position.

    Uses AppleScript + System Events to enumerate windows.
    Returns list of {"app": str, "title": str, "frontmost": bool}.
    """
    # Use a simpler approach that's more permission-friendly
    script = """
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)

        if proc.returncode != 0:
            log.warning(f"get_active_windows failed: {stderr.decode()[:200]}")
            return []

        windows = []
        for line in stdout.decode().strip().split("\n"):
            parts = line.strip().split("|||")
            if len(parts) >= 3:
                windows.append({
                    "app": parts[0].strip(),
                    "title": parts[1].strip(),
                    "frontmost": parts[2].strip().lower() == "true",
                })
        return windows

    except asyncio.TimeoutError:
        log.warning("get_active_windows timed out")
        return []
    except Exception as e:
        log.warning(f"get_active_windows error: {e}")
        return []


async def get_running_apps() -> list[str]:
    """Get list of running application names (visible only)."""
    script = """
tell application "System Events"
    set appNames to name of every application process whose visible is true
    set output to ""
    repeat with a in appNames
        set output to output & a & linefeed
    end repeat
    return output
end tell
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return [a.strip() for a in stdout.decode().strip().split("\n") if a.strip()]
        return []
    except Exception as e:
        log.warning(f"get_running_apps error: {e}")
        return []


async def take_screenshot(display_only: bool = True) -> str | None:
    """Take a screenshot and return base64-encoded PNG.

    Args:
        display_only: If True, capture main display only. If False, all displays.

    Returns:
        Base64-encoded PNG string, or None on failure.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    try:
        cmd = ["screencapture", "-x"]  # -x = no sound
        if display_only:
            cmd.append("-m")  # main display only
        cmd.append(tmp_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)

        if proc.returncode != 0 or not Path(tmp_path).exists():
            log.warning("Screenshot capture failed")
            return None

        data = Path(tmp_path).read_bytes()
        log.info(f"Screenshot captured: {len(data)} bytes")
        return base64.b64encode(data).decode()

    except asyncio.TimeoutError:
        log.warning("Screenshot timed out")
        return None
    except Exception as e:
        log.warning(f"Screenshot error: {e}")
        return None
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


async def save_screenshot_to_desktop(filename: str = "") -> dict:
    """Take a screenshot and save it directly to the Desktop."""
    desktop = Path.home() / "Desktop"
    if not filename:
        filename = f"JARVIS_Screenshot_{int(time.time())}.png"
    if not filename.endswith(".png"):
        filename += ".png"
    out_path = desktop / filename

    try:
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", "-m", str(out_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=8)
        if proc.returncode == 0 and out_path.exists():
            return {"success": True, "path": str(out_path), "confirmation": f"Saved screenshot to your Desktop as {filename}, sir."}
    except Exception as e:
        log.warning(f"save_screenshot_to_desktop error: {e}")

    return {"success": False, "path": None, "confirmation": "Could not capture screenshot, sir."}


async def describe_screen(brain) -> str:
    """Describe what's on the user's screen using the Hybrid Brain."""
    use_local = os.getenv("USE_LOCAL_BRAIN", "false").lower() == "true"
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    # If cloud vision is explicitly available and not local mode, try screenshot + vision
    if not use_local and gemini_key:
        screenshot_b64 = await take_screenshot()
        if screenshot_b64:
            try:
                system = (
                    "You are FixO — an elite AI voice assistant. "
                    "Analyze this screenshot and describe what is on the screen in 1-2 concise, elegant sentences. "
                    "Address the user as sir. Never output thinking or markdown."
                )
                response = await brain.generate(
                    system=system,
                    max_tokens=200,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "What's on my screen right now?",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{screenshot_b64}"
                                },
                            },
                        ],
                    }],
                )
                return response.choices[0].message.content
            except Exception as e:
                log.warning(f"Vision call failed, falling back to window list: {e}")

    # Fallback / Local mode: get window list and have local LLM summarize
    windows = await get_active_windows()
    apps = await get_running_apps()

    if not windows and not apps:
        return "I wasn't able to see your screen, sir. Screen recording permission may be needed."

    # Build a text description for LLM to summarize
    context_parts = []
    if windows:
        for w in windows:
            marker = " (ACTIVE)" if w["frontmost"] else ""
            context_parts.append(f"{w['app']}: {w['title']}{marker}")

    if apps:
        window_apps = set(w["app"] for w in windows) if windows else set()
        bg_apps = [a for a in apps if a not in window_apps]
        if bg_apps:
            context_parts.append(f"Background apps: {', '.join(bg_apps)}")

    if context_parts:
        try:
            system = (
                "You are FixO — an elite AI voice assistant. "
                "Summarize what the user is working on based on the open windows and apps in 1-2 concise sentences. "
                "Address the user as sir. Do not output markdown or thinking traces."
            )
            response = await brain.generate(
                system=system,
                max_tokens=100,
                messages=[{"role": "user", "content": "Open windows:\n" + "\n".join(context_parts)}],
            )
            return response.choices[0].message.content
        except Exception as e:
            log.warning(f"Screen text summary generation failed: {e}")

    # Raw fallback
    if windows:
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {len(windows)} windows open across {len(set(w['app'] for w in windows))} apps."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result

    return f"Running apps: {', '.join(apps)}. Couldn't read window titles, sir."


def format_windows_for_context(windows: list[dict]) -> str:
    """Format window list as context string for the LLM."""
    if not windows:
        return ""
    lines = ["Currently open on your desktop:"]
    for w in windows:
        marker = " (active)" if w["frontmost"] else ""
        lines.append(f"  - {w['app']}: {w['title']}{marker}")
    return "\n".join(lines)
