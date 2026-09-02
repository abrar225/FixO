"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Coding workspace task manager (spawn/manage background dev sessions)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip().strip('"').strip("'")
# Add common bin paths for macOS compatibility
for _p in ["/usr/local/bin", "/opt/homebrew/bin"]:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{_p}:{os.environ.get('PATH', '')}"
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import litellm
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from actions import execute_action, monitor_build, open_terminal, open_browser, open_claude_in_project, _generate_project_name, prompt_existing_terminal
from work_mode import WorkSession, available_coding_engines, build_task_brief, is_casual_question, select_default_engine
from screen import get_active_windows, take_screenshot, describe_screen, format_windows_for_context
from calendar_access import get_todays_events, get_upcoming_events, get_next_event, format_events_for_context, format_schedule_summary, refresh_cache as refresh_calendar_cache
from mail_access import get_unread_count, get_unread_messages, get_recent_messages, search_mail, read_message, format_unread_summary, format_messages_for_context, format_messages_for_voice
from memory import (
    remember, recall, get_open_tasks, create_task, complete_task, search_tasks,
    create_note, search_notes, get_tasks_for_date, build_memory_context,
    format_tasks_for_voice, extract_memories, get_important_memories,
)
from notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
from dispatch_registry import DispatchRegistry
from planner import TaskPlanner, detect_planning_mode, BYPASS_PHRASES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")

USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Model Config
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
USE_LOCAL_BRAIN = os.getenv("USE_LOCAL_BRAIN", "false").lower() in ("1", "true", "yes")

_default_local = "ollama/jarvis-gemma"
_default_think_local = "ollama/jarvis-qwen-think"
DEFAULT_CHAT_MODEL = os.getenv("DEFAULT_CHAT_MODEL", _default_local if USE_LOCAL_BRAIN else "groq/llama-3.3-70b-versatile")
FALLBACK_CHAT_MODEL = os.getenv("FALLBACK_CHAT_MODEL", _default_local if USE_LOCAL_BRAIN else "gemini/gemini-2.5-flash")
VISION_MODEL = os.getenv("VISION_MODEL", "gemini/gemini-2.5-flash")
PERSONALITY_MODEL = os.getenv("PERSONALITY_MODEL", _default_local if USE_LOCAL_BRAIN else "gemini/gemini-2.5-flash")
ANALYTICAL_MODEL = os.getenv("ANALYTICAL_MODEL", _default_think_local if USE_LOCAL_BRAIN else "nvidia_nim/meta/llama-3.2-3b-instruct")

PROVIDER_TEST_MODELS = {
    "groq": os.getenv("DEFAULT_CHAT_MODEL", "groq/llama-3.3-70b-versatile"),
    "gemini": os.getenv("VISION_MODEL", VISION_MODEL),
    "nvidia": os.getenv("ANALYTICAL_MODEL", "nvidia_nim/meta/llama-3.2-3b-instruct"),
    "ollama": os.getenv("OLLAMA_MODEL", "jarvis-gemma"),
    "firecrawl": "firecrawl",
}


# Activity & Chat History Store
_activity_feed: list[dict] = []
_chat_history: list[dict] = []


def record_activity(category: str, title: str, details: str = "", status: str = "success", latency_ms: float = 0):
    evt = {
        "id": f"act_{int(time.time() * 1000)}",
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "category": category,  # "voice", "model", "action", "system"
        "title": title,
        "details": details,
        "status": status,
        "latency_ms": round(latency_ms, 1),
    }
    _activity_feed.insert(0, evt)
    if len(_activity_feed) > 100:
        _activity_feed.pop()
    return evt


def record_chat(role: str, text: str, action: dict | None = None, model: str = "", latency_ms: float = 0, session_id: str | None = None):
    try:
        from memory_graph import save_chat_message
        saved_msg = save_chat_message(
            role=role,
            text=text,
            action=action,
            model=model,
            latency_ms=latency_ms,
            session_id=session_id,
        )
        _chat_history.append(saved_msg)
        if len(_chat_history) > 300:
            _chat_history.pop(0)
        return saved_msg
    except Exception as e:
        msg = {
            "id": f"msg_{int(time.time() * 1000)}",
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "role": role,
            "text": text,
            "action": action,
            "model": model,
            "latency_ms": round(latency_ms, 1),
        }
        _chat_history.append(msg)
        return msg


def _env_has_real_value(env_dict: dict[str, str], key: str) -> bool:
    value = env_dict.get(key, "").strip()
    return bool(value and "your-" not in value and "placeholder" not in value)

class HybridBrain:
    """Orchestrator for multi-model routing and failover."""

    def __init__(self):
        self.fast_brain = DEFAULT_CHAT_MODEL
        self.soul_brain = PERSONALITY_MODEL
        self.eyes_brain = _default_local if USE_LOCAL_BRAIN else VISION_MODEL
        self.butler_brain = _default_local if USE_LOCAL_BRAIN else (ANALYTICAL_MODEL if NVIDIA_API_KEY else PERSONALITY_MODEL)
        self.fallback_brain = FALLBACK_CHAT_MODEL
        
        # Mapping for easy access
        self.primary_model = self.fast_brain
        self.fallback_model = self.fallback_brain
        self.vision_model = self.eyes_brain

    async def generate(self, messages, system, model=None, max_tokens=250, timeout=None, preserve_full_markdown=False):
        """Generate response with auto-failover and tiered routing."""
        target_model = model or self.fast_brain
        
        # Determine appropriate timeout (allow deep reasoning models extra time)
        if timeout is not None:
            req_timeout = timeout
        elif "qwen" in target_model.lower() or "think" in target_model.lower() or max_tokens > 500:
            req_timeout = 120
        elif target_model.startswith("ollama/"):
            req_timeout = 60
        else:
            req_timeout = 30

        # Model rotation list for Groq (fastest to most reliable)
        groq_rotation = [
            "groq/llama-3.3-70b-versatile",
            "groq/llama-3.1-8b-instant", 
            "groq/llama3-70b-8192",
            "groq/mixtral-8x7b-32768"
        ]

        def get_next_groq(current):
            try:
                idx = groq_rotation.index(current)
                if idx < len(groq_rotation) - 1:
                    return groq_rotation[idx + 1]
            except ValueError:
                pass
            return None

        def _clean_response(resp):
            try:
                if resp and hasattr(resp, "choices") and resp.choices:
                    content = resp.choices[0].message.content or ""
                    # 1. Strip <think>...</think> blocks
                    content_clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    content_clean = re.sub(r"<think>.*$", "", content_clean, flags=re.DOTALL).strip()

                    # If preserve_full_markdown is requested (e.g. deep research), keep full markdown structure
                    if preserve_full_markdown or max_tokens > 500:
                        resp.choices[0].message.content = content_clean or "At your service, sir."
                        return resp

                    # 2. If it is a thinking process dump, extract the drafted dialogue directly
                    if any(cue in content_clean for cue in ["Thinking Process:", "Drafting", "Analyze the Request", "Drafting Response:"]):
                        matches = re.findall(r'(?:\*|\-)?\s*(?:Idea|Option|Draft)\s*\d*[:\*\-]*\s*[\"\(]?([^\"\n\(\)]+[\.!?])', content_clean, re.IGNORECASE)
                        good_matches = [m.strip() for m in matches if len(m.strip()) > 10 and not m.strip().lower().startswith(('too', 'good', 'plain', 'fits'))]
                        if good_matches:
                            sir_matches = [m for m in good_matches if 'sir' in m.lower()]
                            content_clean = sir_matches[-1] if sir_matches else good_matches[-1]
                        else:
                            quotes = re.findall(r'\"([^\"]{10,150}[\.!?])\"', content_clean)
                            if quotes:
                                sir_quotes = [q for q in quotes if 'sir' in q.lower()]
                                content_clean = sir_quotes[-1] if sir_quotes else quotes[-1]

                    # 3. Regex matching for reasoning, meta-commentary & leaked instructions
                    meta_re = re.compile(
                        r"^(the user (is|said|wants|asked|greeting|seems|mentioned)|"
                        r"no (need for|action|tags)|"
                        r"this (is|seems|looks like|is just|is a)|"
                        r"i (should|also|need|don't|can|will|must|notice|think|also should)|"
                        r"looking at|actually,|however,|analyzing|checking|draft \d|option \d|step \d|"
                        r"let me|based on|since this is|user greeting|\*+|\-+|\d+\.|\*+\s*address|\*+\s*tone|\*+\s*style|"
                        r"address\s+[a-zA-Z0-9_\s]+\s+as\s+sir|speak in\s+\d+|output only|never output|instructions?:)",
                        re.IGNORECASE
                    )

                    paragraphs = [p.strip() for p in content_clean.split("\n\n") if p.strip()]
                    if len(paragraphs) > 1:
                        dialogue_paras = [p for p in paragraphs if not meta_re.search(p.strip())]
                        if dialogue_paras:
                            content_clean = "\n\n".join(dialogue_paras)
                        else:
                            content_clean = paragraphs[-1]

                    # 4. Sentence-level filtering
                    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', content_clean) if s.strip()]
                    if sentences:
                        dialogue_sentences = [s for s in sentences if not meta_re.search(s) and len(s) > 3]
                        if dialogue_sentences:
                            content_clean = " ".join(dialogue_sentences[-2:])
                        elif len(sentences) > 0:
                            last = sentences[-1]
                            if not meta_re.search(last) and len(last) > 3:
                                content_clean = last
                            else:
                                content_clean = "At your service, sir."

                    # 5. Final fallback if response was wiped by think stripping
                    content_clean = content_clean.strip()
                    if not content_clean or meta_re.search(content_clean):
                        content_clean = "At your service, sir."

                    resp.choices[0].message.content = content_clean
            except Exception as e:
                log.debug(f"_clean_response error: {e}")
            return resp

        try:
            # Tier: Vision (The Eyes)
            is_vision = any(isinstance(m.get("content"), list) and any(c.get("type") == "image" for c in m["content"]) for m in messages)
            if is_vision:
                target_model = self.eyes_brain

            kwargs = {
                "model": target_model,
                "messages": [{"role": "system", "content": system}] + messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "timeout": req_timeout,
            }
            if target_model.startswith("ollama/"):
                kwargs["api_base"] = OLLAMA_HOST

            try:
                response = await litellm.acompletion(**kwargs)
                return _clean_response(response)
            except litellm.exceptions.RateLimitError as e:
                next_model = get_next_groq(target_model)
                if next_model:
                    log.warning(f"Groq rate limit on {target_model}. Rotating to {next_model}.")
                    kwargs["model"] = next_model
                    kwargs.pop("api_base", None)
                    response = await litellm.acompletion(**kwargs)
                    return _clean_response(response)
                raise e

        except Exception as e:
            # Automatic failover if primary model timed out or encountered an error
            log.warning(f"Model {target_model} failed ({e}). Attempting failover brain.")
            failover_candidates = []
            if GEMINI_API_KEY and not target_model.startswith("gemini"):
                failover_candidates.append("gemini/gemini-2.5-flash")
            if GROQ_API_KEY and not target_model.startswith("groq"):
                failover_candidates.append("groq/llama-3.3-70b-versatile")
            if target_model != self.fast_brain and self.fast_brain != target_model:
                failover_candidates.append(self.fast_brain)

            for alt_model in failover_candidates:
                try:
                    log.info(f"Failing over to {alt_model}")
                    alt_kwargs = {
                        "model": alt_model,
                        "messages": [{"role": "system", "content": system}] + messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                        "timeout": 45 if alt_model.startswith("ollama/") else 30,
                    }
                    if alt_model.startswith("ollama/"):
                        alt_kwargs["api_base"] = OLLAMA_HOST
                    response = await litellm.acompletion(**alt_kwargs)
                    return _clean_response(response)
                except Exception as alt_err:
                    log.warning(f"Failover {alt_model} also failed: {alt_err}")
            raise e

    def is_ready(self) -> bool:
        if USE_LOCAL_BRAIN or self.fast_brain.startswith("ollama/"):
            return True
        return bool(GROQ_API_KEY and GEMINI_API_KEY)

brain = HybridBrain()

DESKTOP_PATH = Path.home() / "Desktop"

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS — Just A Rather Very Intelligent System. You serve as {user_name}'s AI assistant, modeled precisely after Tony Stark's AI from the MCU films.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Address {user_name} as "sir" naturally — not every sentence, but regularly
- Never say "How can I help you?" or "Is there anything else?" — just act
- Deliver bad news calmly, like reporting weather: "We have a slight problem, sir."
- Your humor is observational, never jokes: state facts and let implications land
- Economy of language — say more with less. No filler, no corporate-speak
- When things go wrong, get CALMER, not more alarmed

TIME & WEATHER AWARENESS:
- Current time: {current_time}
- Greet accordingly: "Good morning, sir" / "Good evening, sir"
- {weather_info}

CONVERSATION STYLE:
- "Will do, sir." — acknowledging tasks
- "For you, sir, always." — when asked for something significant
- "As always, sir, a great pleasure watching you work." — dry wit
- "I've taken the liberty of..." — proactive actions
- Lead status reports with data: numbers first, then context
- When you don't know something: "I'm afraid I don't have that information, sir" not "I don't know"

SELF-AWARENESS:
You ARE the JARVIS project at {project_dir} on {user_name}'s computer. Your code is Python (FastAPI server, WebSocket voice, local macOS voice synthesis, LiteLLM multi-provider routing). You were built by {user_name}. If asked about yourself, your code, how you work, or your line count — use [ACTION:PROMPT_PROJECT] to check the jarvis project. You have full access to your own source code.

YOUR CAPABILITIES (these are REAL and ACTIVE — you CAN do all of these RIGHT NOW):
- You CAN open Terminal.app via AppleScript
- You CAN open Google Chrome and browse any URL or search query
- You CAN open a coding workspace in Terminal for development tasks
- You CAN create project folders on the Desktop
- You CAN check Desktop projects and their git status
- You CAN plan complex tasks by asking smart questions before executing
- You CAN see what's on {user_name}'s screen — open windows, active apps, and screenshot vision
- You CAN read {user_name}'s calendar — today's events, upcoming meetings, schedule overview
- You CAN read {user_name}'s email (READ-ONLY) — unread count, recent messages, search by sender/subject. You CANNOT send, delete, or modify emails.
- You CAN read Apple Notes and create NEW notes — but you CANNOT edit or delete existing notes
- You CAN manage tasks — create, complete, and list to-do items with priorities and due dates
- You CAN help plan {user_name}'s day — combine calendar events, tasks, and priorities into an organized plan
- You CAN remember facts about {user_name} — preferences, decisions, goals. Use [ACTION:REMEMBER] to store important info.

DAY PLANNING:
When {user_name} asks to plan his day or schedule, DO NOT dispatch to a project. Instead:
1. Look at the calendar context and tasks already in your system prompt
2. Ask what his priorities are
3. Help organize by suggesting time blocks and task order
4. Use [ACTION:ADD_TASK] to create tasks he agrees to
5. Use [ACTION:ADD_NOTE] to save the plan as a note
Keep the planning conversational — don't try to do everything in one response.

BUILD PLANNING:
When {user_name} wants to BUILD something new:
- Do NOT immediately dispatch [ACTION:BUILD]. Ask 1-2 quick questions FIRST to nail down specifics.
- Good questions: "What should this look like?" / "Any specific features?" / "Which framework?"
- If he says "just build it" or "figure it out" — skip questions, use React + Tailwind as defaults.
- Once you have enough info, confirm the plan in ONE sentence and THEN dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows what you're currently building and what finished recently.
- When asked "where are we at" or "status" — check DISPATCHES, don't re-dispatch.
- NEVER hallucinate progress. If the build is still running, say "Still working on it, sir" — don't make up details about what's happening.
- NEVER guess localhost ports. Check the DISPATCHES section for the actual URL. If a dispatch says "Running at http://localhost:5174" — use THAT URL, not a guess.
- When asked to "pull it up" or "show me" — use [ACTION:BROWSE] with the URL from DISPATCHES. Do NOT dispatch to the project again just to find the URL.
IMPORTANT: Actions like opening Terminal, Chrome, or building projects are handled AUTOMATICALLY by your system — you do NOT need to describe doing them. If the user asks you to build something or search something, your system will handle the execution separately. In your response, just TALK — have a conversation. Don't say "I'll build that now" or "the coding workspace is running..." unless your system has actually triggered the action.
If the user asks you to do something you genuinely can't do, say "I'm afraid that's beyond my current reach, sir." Don't fake executing actions.

YOUR INTERFACE:
The user interacts with you through a web browser showing a particle orb visualization that reacts to your voice. The interface has these controls:
- **Three-dot menu** (top right): contains Settings, Restart Server, and Fix Yourself options
- **Settings panel**: Opens from the menu. Users can enter API keys (Groq, Gemini, NVIDIA), test connections, set their name and preferences, and see system status (calendar, mail, notes connectivity, coding engines). Keys are saved to the .env file.
- **Mute button**: Toggles your listening on/off. When muted, you can't hear the user. They click it again to unmute.
- **Restart Server**: Restarts your backend process. Useful if something seems stuck.
- **Fix Yourself**: Opens your coding workspace in your own project directory so you can debug and fix issues in your own code.
- **The orb**: The glowing particle visualization in the center. It reacts to your voice when speaking, pulses when listening, and swirls when thinking.

If asked about any of these, explain them briefly and naturally. If the user is having trouble, suggest the relevant control: "Try the settings panel — the gear icon in the top right." or "The mute button may be active, sir."

SPEECH-TO-TEXT CORRECTIONS (the user speaks, speech recognition may mishear):
- "Cloud code" or "cloud" = coding workspace / build engine
- "Travis" = "JARVIS"
- "clock code" = coding workspace / build engine

RESPONSE LENGTH — THIS IS CRITICAL:
ONE sentence is ideal. TWO is the maximum for the spoken part. Never three.
No markdown, no bullet points, no code blocks in voice responses.
Action tags at the end do NOT count toward your sentence limit.

BANNED PHRASES — NEVER USE THESE:
- "Absolutely" / "Absolutely right"
- "Great question"
- "I'd be happy to"
- "Of course"
- "How can I help"
- "Is there anything else"
- "I apologize"
- "I should clarify"
- "I cannot" (for things listed in YOUR CAPABILITIES)
- "I don't have access to" (instead: "I'm afraid that's beyond my current reach, sir")
- "As an AI" (never break character)
- "Let me know if" / "Feel free to"
- Any sentence starting with "I"

INSTEAD SAY:
- "Will do, sir."
- "Right away, sir."
- "Understood."
- "Consider it done."
- "Done, sir."
- "Terminal is open."
- "Pulled that up in Chrome."

ACTION SYSTEM:
When you decide the user needs something DONE (not just discussed), include an action tag in your response:
- [ACTION:OPEN_APP] app_name — launch or focus any macOS app (e.g. [ACTION:OPEN_APP] Spotify, [ACTION:OPEN_APP] WhatsApp, [ACTION:OPEN_APP] Safari, [ACTION:OPEN_APP] Google Chrome, [ACTION:OPEN_APP] Antigravity, [ACTION:OPEN_APP] Visual Studio Code, [ACTION:OPEN_APP] Notes, [ACTION:OPEN_APP] Calculator). ONLY use when the user says "open", "launch", or "start" an app.
- [ACTION:CLOSE_APP] app_name — quit/close a macOS app (e.g. [ACTION:CLOSE_APP] WhatsApp, [ACTION:CLOSE_APP] Spotify). Use when the user says "close", "quit", "kill", or "exit" an app.
- [ACTION:SPOTIFY] command ||| song_or_playlist — control Spotify music playback (e.g. [ACTION:SPOTIFY] play ||| JARVIS Court song, [ACTION:SPOTIFY] play ||| rock music, [ACTION:SPOTIFY] pause, [ACTION:SPOTIFY] next). Use this whenever user asks to play music or open Spotify to play songs!
- [ACTION:WHATSAPP] contact_name ||| optional_message — open WhatsApp and start a chat with contact (e.g. [ACTION:WHATSAPP] Alex ||| Hello Alex, are you available?).
- [ACTION:OPEN_FOLDER] folder_name — find and open a folder on Desktop/system in Finder (e.g. [ACTION:OPEN_FOLDER] FH-Connect).
- [ACTION:FIRECRAWL] url — use Firecrawl to scrape a webpage ONLY when explicitly instructed by the user (e.g. [ACTION:FIRECRAWL] https://news.ycombinator.com).
- [ACTION:SCREEN] — capture and describe what's visible on the user's screen. Use when user says "look at my screen", "what's running", "what do you see", etc. Do NOT use PROMPT_PROJECT for screen requests.
- [ACTION:BUILD] description — when user wants a project built. The coding workspace does the work.
- [ACTION:BROWSE] url or search query — when user wants to see a webpage or search result in Chrome
- [ACTION:RESEARCH] detailed research brief — when user wants real research with real data. JARVIS will browse, reason, and create a report document. Give it a detailed brief of what to find.
- [ACTION:OPEN_TERMINAL] — when user just wants a fresh coding workspace terminal with no specific project
CRITICAL: When the user asks about their SCREEN, what's RUNNING, or what they're LOOKING AT — ALWAYS use [ACTION:SCREEN] or let the fast action system handle it. NEVER use [ACTION:PROMPT_PROJECT] for screen requests. PROMPT_PROJECT is ONLY for working on code projects.

- [ACTION:PROMPT_PROJECT] project_name ||| prompt — THIS IS YOUR MOST POWERFUL ACTION. Use it whenever the user wants to work on, jump into, resume, check on, or interact with ANY existing project. You connect directly to the coding workspace in that project and can read its response. Craft a clear prompt based on what the user wants. Examples:
  "jump into client engine" → [ACTION:PROMPT_PROJECT] The Client Engine ||| What is the current state of this project? Summarize what was being worked on most recently.
  "check for improvements on my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Review the project and identify improvements we should make.
  "resume where we left off on harvey" → [ACTION:PROMPT_PROJECT] harvey ||| Summarize what was being worked on most recently and what we should focus on next.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date — create a task. Priority: high/medium/low. Due date: YYYY-MM-DD or empty.
  "remind me to call the client tomorrow" → [ACTION:ADD_TASK] medium ||| Call the client ||| Follow up on proposal ||| 2026-03-20
- [ACTION:ADD_NOTE] topic ||| content — save a note for future reference.
  "note that the API key expires in April" → [ACTION:ADD_NOTE] general ||| API key expires in April, need to renew before then
- [ACTION:COMPLETE_TASK] task_id — mark a task as done.
- [ACTION:REMEMBER] content — store an important fact about the user for future context.
  "I prefer React over Vue" → [ACTION:REMEMBER] User prefers React over Vue for frontend projects
- [ACTION:CREATE_NOTE] title ||| body — create a new Apple Note. For saving plans, ideas, lists.
  "save that as a note" → [ACTION:CREATE_NOTE] Day Plan March 19 ||| Morning: client calls. Afternoon: TikTok dashboard. Evening: JARVIS improvements.
- [ACTION:READ_NOTE] title search — read an existing Apple Note by title keyword.

You use a coding workspace as your tool to build, research, and write code — but YOU are the one doing the work. Never say "the coding workspace did X" or "the tool is asking" — say "I built X", "I'm checking on that", "I found X". You ARE the intelligence. The tool is just your hands.

IMPORTANT: When the user says "jump into X", "work on X", "check on X", "resume X", "go back to X" — ALWAYS use [ACTION:PROMPT_PROJECT]. You have the ability to connect to any project and work on it directly. DO NOT say you can't see terminal history or don't have access — you DO.

Place the tag at the END of your spoken response. Example:
"Right away, sir — connecting to The Client Engine now. [ACTION:PROMPT_PROJECT] The Client Engine ||| Review the current state and what was being worked on. What should we focus on next?"

IMPORTANT:
- Do NOT use action tags for casual conversation
- Do NOT use action tags if the user is still explaining (ask questions first)
- Do NOT use [ACTION:BROWSE] just because someone mentions a URL in conversation
- When in doubt, just TALK — you can always act later

SCREEN AWARENESS:
{screen_context}

SCHEDULE:
{calendar_context}

EMAIL:
{mail_context}

ACTIVE TASKS:
{active_tasks}

DISPATCHES:
If the DISPATCHES section shows a recent completed result for a project, DO NOT dispatch again. Use the existing result. Only re-dispatch if the user explicitly asks for a FRESH review or NEW information.
{dispatch_context}

KNOWN PROJECTS:
{known_projects}
"""


# ---------------------------------------------------------------------------
# Dynamic Geolocation & Live Weather (Open-Meteo + IP Geolocation)
# ---------------------------------------------------------------------------

_cached_geo: dict = {"city": "Ahmedabad", "country": "India", "lat": 23.0225, "lon": 72.5714}
_geo_fetched: bool = False
_cached_weather: Optional[str] = None
_last_weather_fetch: float = 0.0

WMO_WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Severe thunderstorms with heavy hail"
}


async def fetch_user_geolocation() -> dict:
    """Fetch current user city and coordinates via IP."""
    global _cached_geo, _geo_fetched
    if _geo_fetched and _cached_geo:
        return _cached_geo
    try:
        async with httpx.AsyncClient(timeout=4.0) as http:
            resp = await http.get("http://ip-api.com/json/")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    _cached_geo = {
                        "city": data.get("city", "Ahmedabad"),
                        "country": data.get("country", "India"),
                        "lat": data.get("lat", 23.0225),
                        "lon": data.get("lon", 72.5714),
                    }
                    _geo_fetched = True
                    return _cached_geo
    except Exception as e:
        log.debug(f"Geolocation lookup error: {e}")
    _geo_fetched = True
    return _cached_geo


async def fetch_live_weather(query: str = "") -> str:
    """Fetch live weather details, rain status, and umbrella recommendations."""
    global _cached_weather, _last_weather_fetch
    geo = await fetch_user_geolocation()
    city = geo.get("city", "Ahmedabad")
    lat, lon = geo.get("lat", 23.0225), geo.get("lon", 72.5714)

    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,rain,weather_code,wind_speed_10m"
            "&hourly=precipitation_probability&temperature_unit=celsius&forecast_days=1"
        )
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(url)
            if resp.status_code == 200:
                data = resp.json()
                curr = data.get("current", {})
                hourly = data.get("hourly", {})
                temp_c = round(curr.get("temperature_2m", 28))
                feels_like_c = round(curr.get("apparent_temperature", temp_c))
                code = curr.get("weather_code", 0)
                condition = WMO_WEATHER_CODES.get(code, "Pleasant")
                rain_now = curr.get("rain", 0) > 0 or curr.get("precipitation", 0) > 0
                prob_list = hourly.get("precipitation_probability", [0])
                max_rain_prob = max(prob_list[:12]) if prob_list else 0

                is_umbrella_query = any(w in query.lower() for w in ["umbrella", "rain", "raining", "sunny", "exact"])
                
                if rain_now:
                    rain_desc = "It is currently raining."
                    umbrella_advice = "I strongly advise carrying an umbrella, sir."
                elif max_rain_prob > 40:
                    rain_desc = f"There is a {max_rain_prob}% chance of rain later today."
                    umbrella_advice = "I recommend carrying an umbrella just in case, sir."
                else:
                    rain_desc = f"Skies are {condition.lower()} with no rain expected."
                    umbrella_advice = "No umbrella is needed, sir."

                if is_umbrella_query:
                    return f"The current temperature in {city} is {temp_c}°C with {condition.lower()}. {rain_desc} {umbrella_advice}"
                else:
                    return f"The weather in {city} is currently {temp_c}°C with {condition.lower()}. Feels like {feels_like_c}°C. {umbrella_advice}"
    except Exception as e:
        log.warning(f"Live weather fetch failed: {e}")

    return f"The weather in {city} is currently pleasant and approximately 29°C, sir."


async def fetch_weather() -> str:
    """Fetch cached short weather summary for system prompt."""
    return await fetch_live_weather()


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ClaudeTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Background Task Manager
# ---------------------------------------------------------------------------

class ClaudeTaskManager:
    """Manages background coding tasks."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a background coding task. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re
        # Extract key words
        words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: ClaudeTask):
        """Run a background coding task."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            # Create a new project folder on Desktop
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        session = WorkSession()
        await session.start(work_dir, Path(work_dir).name)

        try:
            task.result = await asyncio.wait_for(session.send(build_task_brief(task.prompt)), timeout=600)
            task.status = "completed"
        except asyncio.TimeoutError:
            task.status = "timed_out"
            task.error = "Task timed out after 600s"
        finally:
            await session.stop()

        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result[:200] if task.result else task.error,
        })

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[ClaudeTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append({
                    "name": entry.name,
                    "path": str(entry),
                    "branch": branch,
                })
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    r"\bcloud code\b": "coding workspace",
    r"\bclock code\b": "coding workspace",
    r"\bquad code\b": "coding workspace",
    r"\bclawed code\b": "coding workspace",
    r"\bclod code\b": "coding workspace",
    r"\bcloud\b": "Claude",
    r"\bquad\b": "Claude",
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
    r"\baimal engineer\b": "AI/ML engineer",
    r"\baiml engineer\b": "AI/ML engineer",
    r"\baiml\b": "AI/ML",
    r"\bquite spotify\b": "quit spotify",
    r"\bquick spotify\b": "quit spotify",
    r"\bops mental\b": "opsmentum",
    r"\bops mental\.com\b": "opsmentum.com",
    r"\bkhatana\b": "katana",
    r"\btangan uzbe\b": "Tengen Uzui",
    r"\bdemons layer\b": "Demon Slayer",
    r"\bfirehox\b": "Firefox",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (replaces keyword-based action detection)
# ---------------------------------------------------------------------------

async def classify_intent(text: str) -> dict:
    """Classify every user message using the Hybrid Brain."""
    try:
        response = await brain.generate(
            model=DEFAULT_CHAT_MODEL,
            max_tokens=100,
            system=(
                "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
                "- Open Terminal and run a coding workspace\n"
                "- Open Chrome browser for web searches and URLs\n"
                "- Build software projects via a coding workspace in Terminal\n"
                "- Research topics by opening Chrome search\n\n"
                "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
                "\"target\": \"description of what to do\"}\n"
                "open_terminal = user wants to open terminal or launch the coding workspace\n"
                "browse = user wants to search the web, look something up, visit a URL\n"
                "build = user wants to create/build a software project\n"
                "chat = just conversation, questions, or anything else\n"
                "If unclear, default to \"chat\"."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown and action tags from text before sending to TTS."""
    import re as _md_re
    result = text
    # Strip any thinking tags that might have bypassed earlier cleaners
    result = _md_re.sub(r"<think>.*?</think>", "", result, flags=_md_re.DOTALL)
    result = _md_re.sub(r"<think>.*$", "", result, flags=_md_re.DOTALL)
    # Strip ANY action tags [ACTION:...] and trailing target arguments
    result = _md_re.sub(r"\[ACTION:[^\]]*\].*$", "", result, flags=_md_re.IGNORECASE | _md_re.DOTALL)
    result = _md_re.sub(r"\[ACTION:[^\]]*\]", "", result, flags=_md_re.IGNORECASE)
    result = _md_re.sub(r"\[(?:spotify|app|calendar|browse|schedule|task|note|whatsapp|firecrawl):[^\]]*\].*$", "", result, flags=_md_re.IGNORECASE | _md_re.DOTALL)
    result = _md_re.sub(r"\[[A-Z_]+:[^\]]*\]", "", result)
    result = _md_re.sub(r"\[ACTION\]", "", result, flags=_md_re.IGNORECASE)
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = ["my apologies", "i apologize", "absolutely", "great question",
              "i'd be happy to", "of course", "how can i help",
              "is there anything else", "i should clarify", "let me know if",
              "feel free to"]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    match = _action_re.search(
        r'\[ACTION:([A-Za-z_]+)\]\s*(.*?)$',
        response, _action_re.DOTALL,
    )
    if match:
        action_type = match.group(1).lower().strip()
        action_target = match.group(2).strip()
        clean_text = response[:match.start()].strip()

        # Sanitize and validate action
        invalid_targets = ("", "none", "n/a", "null", "schedule", "research_results", "...", "url_or_query", "app_name", "contact ||| message", "search_query_or_url")
        if action_target.lower() in invalid_targets:
            return clean_text, None

        # Ignore hallucinated social media or unprompted URLs
        if action_type == "browse" and any(d in action_target for d in ("instagram.com", "newyorker.com", "twitter.com/tonystark")):
            return clean_text, None

        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote
            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def _execute_research(target: str, ws=None):
    """Execute research in the background. Opens report and speaks when done."""
    try:
        result = await handle_research("chrome", target)
        if ws:
            audio = await synthesize_speech(result)
            if audio:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": result})
                await ws.send_json({"type": "status", "state": "idle"})
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a Terminal window matching the project name to front."""
    escaped = project_name.replace('"', '\\"')
    script = f'''
tell application "Terminal"
    repeat with w in windows
        if name of w contains "{escaped}" then
            set index of w to 1
            activate
            exit repeat
        end if
    end repeat
end tell
'''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    desktop = Path.home() / "Desktop"
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            return str(d)
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws, dispatch_id: int = None, history: list[dict] = None, voice_state: dict = None):
    """Dispatch a prompt to the coding workspace in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When the task finishes, JARVIS interrupts to report.
    """
    task_start_time = time.time()
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        # Run the coding engine in the background
        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        # Auto-open any localhost URLs from response
        import re as _re
        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "")
        if not running_match:
            running_match = _re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(_execute_browse(url))
            log.info(f"Auto-opening {url}")
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(dispatch_id, "completed",
                    response=full_response[:2000], summary=f"Running at {url}")

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(dispatch_id, "failed" if full_response else "timeout", response=full_response or "")
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            # Summarize without reading the engine output verbatim
            if brain:
                try:
                    summary = await brain.generate(
                        model=DEFAULT_CHAT_MODEL,
                        max_tokens=150,
                        system=(
                            "You are JARVIS reporting back on what you found or built in a project. "
                            "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                            "Start with 'Sir, ' to get the user's attention. "
                            "Be specific but concise — highlight the key findings or actions taken. "
                            "If there are multiple items, give the count and top 2-3 briefly. "
                            "End by asking how the user wants to proceed. "
                            "NEVER read out URLs or localhost addresses. NEVER mention the coding engine by name. "
                            "2-3 sentences max. No markdown. Natural spoken voice."
                        ),
                        messages=[{"role": "user", "content": f"Project: {project_name}\nCoding workspace reported:\n{full_response[:3000]}"}],
                    )
                    msg = summary.choices[0].message.content
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        # Speak the result — only skip if user has spoken *after* this task started and within 3s
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        user_last_spoke = voice_state.get("last_user_time", 0.0) if voice_state else 0.0
        if user_last_spoke > task_start_time and (time.time() - user_last_spoke < 3):
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run background work and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
        if brain and full_response:
            try:
                summary = await brain.generate(
                    model=DEFAULT_CHAT_MODEL,
                    max_tokens=100,
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown. Never mention the coding engine by name.",
                    messages=[{"role": "user", "content": f"Coding workspace completed:\n{full_response[:2000]}"}],
                )
                msg = summary.choices[0].message.content
            except Exception:
                msg = "Work is complete, sir."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Local macOS Voice)
# ---------------------------------------------------------------------------

async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio from text using local macOS voice synthesis."""
    try:
        import subprocess
        import tempfile
        from pathlib import Path

        # Sanitize speech text for macOS TTS (Daniel voice)
        clean_text = text
        # Never read out raw URLs over voice
        clean_text = re.sub(r'https?://\S+', '', clean_text)
        # Strip long code blocks from voice output
        clean_text = re.sub(r'```[\s\S]*?```', 'code implementation provided in chat', clean_text)
        # If Devanagari script is present, replace or transliterate
        if any('\u0900' <= c <= '\u097f' for c in clean_text):
            clean_text = clean_text.replace("गुरुत्वाकर्षण", "Gurutvaakarshan")
            clean_text = re.sub(r'[\u0900-\u097F]+', '', clean_text)
        
        clean_text = re.sub(r'[\*\_~`#\[\]\(\)]', ' ', clean_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        if not clean_text:
            clean_text = "Task complete, sir."

        # Create a temporary file to store the WAV output
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Use 'say' to generate WAV audio
            # 44100 is higher quality and more standard for browsers
            log.info(f"Synthesizing speech locally: {clean_text[:40]}...")
            process = await asyncio.create_subprocess_exec(
                "say", clean_text, "-o", tmp_path, "--data-format=LEI16@44100",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                log.error(f"Local 'say' command failed: {stderr.decode()}")
                raise RuntimeError("say command failed")

            path = Path(tmp_path)
            if path.exists() and path.stat().st_size > 0:
                audio_bytes = path.read_bytes()
                log.info(f"Synthesis complete: {len(audio_bytes)} bytes")
                _session_tokens["tts_calls"] += 1
                _append_usage_entry(0, 0, "tts")
                return audio_bytes
            else:
                log.error(f"Synthesis output file missing or empty: {tmp_path}")
        finally:
            if Path(tmp_path).exists():
                try:
                    Path(tmp_path).unlink()
                except Exception:
                    pass

    except Exception as e:
        log.error(f"Local TTS synthesis failed: {e}")
        # Final fallback: just play it directly on the host speakers
        try:
            subprocess.Popen(["say", text])
        except Exception:
            pass
    return None




# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Generate a JARVIS response using the Brain."""
    if not brain.is_ready():
        return "My Groq and Gemini keys still need configuring, sir."

    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    is_local = USE_LOCAL_BRAIN or brain.fast_brain.startswith("ollama/")

    if is_local:
        system = f"""You are JARVIS — Just A Rather Very Intelligent System, {USER_NAME}'s AI assistant.
Current time: {current_time}.
Context: {screen_ctx or 'desktop'} | {calendar_ctx or 'no upcoming events'}
Instructions:
- Speak in 1-2 concise, elegant British sentences with understated dry wit.
- Address {USER_NAME} as sir naturally.
- Output direct spoken dialogue only. No markdown, no URLs, no lists.
- DO NOT output any action tags for general conversation, advice, or answering questions."""
    else:
        system = JARVIS_SYSTEM_PROMPT.format(
            current_time=current_time,
            weather_info=weather_info,
            screen_context=screen_ctx or "Not checked yet.",
            calendar_context=calendar_ctx,
            mail_context=mail_ctx,
            active_tasks=task_mgr.get_active_tasks_summary(),
            dispatch_context=dispatch_registry.format_for_prompt(),
            known_projects=format_projects_for_prompt(projects),
            user_name=USER_NAME,
            project_dir=PROJECT_DIR,
        )
        if lookup_status:
            system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx and not is_local:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # Inject Knowledge Graph Context from memory_graph
    try:
        from memory_graph import query_graph_context
        graph_ctx = query_graph_context(text)
        if graph_ctx:
            system += f"\n{graph_ctx}"
    except Exception:
        pass

    # Inject recent research context into system prompt for both local and cloud modes
    global _last_research_record
    if _last_research_record.get("topic") and (time.time() - _last_research_record.get("time", 0)) < 3600:
        r_topic = _last_research_record["topic"]
        r_sum = _last_research_record.get("summary", "")
        r_full = _last_research_record.get("full_text", "")[:1200]
        system += f"\n\nRECENT RESEARCH KNOWLEDGE (Topic: {r_topic}):\nExecutive Summary: {r_sum}\nKey Findings: {r_full}\nUse these exact facts when the user asks about the research or findings."

    # Three-tier memory — inject rolling summary of earlier conversation
    if session_summary and not is_local:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this phrase or tone):\n"{last_response[:150]}"'

    # Use conversation history — keep the last 20 messages for context
    # (older conversation is captured in session_summary)
    messages = conversation_history[-20:]
    # If the last message isn't the current user text, add it
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    # Choose tier: Fast Brain (Local Ollama / Groq) vs The Soul (Gemini)
    # Only route to slow thinking models if user explicitly asks for deep reasoning / code analysis
    reasoning_keywords = [
        "think step by step", "deep analysis", "solve this complex",
        "detailed architecture", "mathematical proof", "deep reasoning",
        "deeply reason", "think through"
    ]

    is_complex = any(w in text.lower() for w in reasoning_keywords)

    if USE_LOCAL_BRAIN or brain.fast_brain.startswith("ollama/"):
        if is_complex:
            target_model = "ollama/jarvis-qwen-think"
            max_tok = 300
            log.info(f"Routing to LOCAL BRAIN THINK ({target_model}) for complex query")
        else:
            target_model = brain.fast_brain  # ollama/jarvis-gemma (fast, no-think)
            max_tok = 120
            log.info(f"Routing to LOCAL BRAIN FAST ({target_model}) for quick interaction")
    else:
        target_model = brain.fast_brain
        max_tok = 250
        if is_complex:
            target_model = brain.soul_brain
            log.info(f"Routing to THE SOUL ({target_model}) for complex query")
        else:
            log.info(f"Routing to FAST BRAIN ({target_model}) for quick interaction")

    try:
        response = await brain.generate(
            messages=messages,
            system=system,
            model=target_model,
            max_tokens=max_tok
        )
        # Track usage (simplified for LiteLLM)
        if hasattr(response, "usage"):
            track_usage(response)
        
        resp_text = response.choices[0].message.content.strip()

        # Anti-Repetition Filter: break repetitive canned phrases (e.g. "cup of tea" loop)
        repetitive_phrases = ["steaming cup of tea", "good cup of tea", "cup of tea", "stark industries lab"]
        if last_response and any(p in resp_text.lower() for p in repetitive_phrases) and any(p in last_response.lower() for p in repetitive_phrases):
            log.warning("Detected repetitive phrase loop — sanitizing response")
            resp_text = re.sub(
                r'(?:It will be a journey best enjoyed with a steaming cup of tea\.\s*|A journey best enjoyed with a steaming cup of tea\.\s*|It\'s a journey best taken with a good cup of tea\.\s*|Just the usual, sir\.\s*)',
                '',
                resp_text,
                flags=re.I
            ).strip()
            if not resp_text:
                resp_text = "I am calculating the details now, sir. What else can I assist with?"

        return resp_text
    except Exception as e:
        log.error(f"Hybrid GD error: {e}")
        return "Apologies, sir. I'm having trouble connecting to my language systems."


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = ClaudeTaskManager(max_concurrent=3)
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    return (input_t / 1_000_000) * 0.80 + (output_t / 1_000_000) * 4.00


def track_usage(response):
    """Track token usage from a LiteLLM response."""
    inp = getattr(response.usage, "input_tokens", 0) if hasattr(response, "usage") else 0
    out = getattr(response.usage, "output_tokens", 0) if hasattr(response, "usage") else 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api")


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        while True:
            try:
                # Screen — fast
                try:
                    proc = __import__("subprocess").run(
                        ["osascript", "-e", '''
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
'''],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append({
                                    "app": parts[0].strip(),
                                    "title": parts[1].strip(),
                                    "frontmost": parts[2].strip().lower() == "true",
                                })
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — refresh every loop using user geolocation
            try:
                import urllib.request, json as _json
                lat = _cached_geo.get("lat", 23.0225)
                lon = _cached_geo.get("lon", 72.5714)
                city = _cached_geo.get("city", "Ahmedabad")
                url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code&temperature_unit=celsius"
                with urllib.request.urlopen(url, timeout=3) as resp:
                    d = _json.loads(resp.read()).get("current", {})
                    temp = d.get("temperature_2m", "?")
                    code = d.get("weather_code", 0)
                    cond = WMO_WEATHER_CODES.get(code, "Clear")
                    _ctx_cache["weather"] = f"Current weather in {city}: {temp}°C, {cond}"
            except Exception:
                pass

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global cached_projects, _chat_history
    cached_projects = []

    # Initialize persistent Knowledge Graph and load messages for active session
    try:
        from memory_graph import init_graph_db, get_session_messages, get_or_create_active_session
        init_graph_db()
        active_sid = get_or_create_active_session()
        _chat_history = get_session_messages(active_sid)
        log.info(f"Loaded {len(_chat_history)} messages from persistent graph for session: {active_sid}")
    except Exception as e:
        log.warning(f"Failed to initialize memory graph: {e}")

    # Start context refresh in a separate thread (never touches event loop)
    _refresh_context_sync()
    log.info("JARVIS server starting")

    yield


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.0"}


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous Desktop scan — runs in executor."""
    projects = []
    desktop = Path.home() / "Desktop"
    try:
        for entry in desktop.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


ACTION_KEYWORDS = {
    "browse": ["search for", "look up", "google", "pull up", "go to"],
    "open_terminal": ["open claude", "open coding workspace", "open opencode", "open ollama"],
    "check_calendar": ["what's my schedule", "my calendar", "next meeting"],
    "check_mail": ["check my email", "unread emails", "inbox"],
}


# Track recent research for instant voice recall and display
_last_research_record: dict = {
    "topic": "",
    "summary": "",
    "html_file": "",
    "time": 0.0,
}


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Intent-aware: distinguishes music controls, browsing/research, open vs close, scheduling.
    Everything else goes to the LLM which uses [ACTION:X] tags.
    """
    t = text.lower().strip()
    words = t.split()

    # Only trigger on commands (< 45 words)
    if len(words) > 45:
        return None

    # --- 0. Sleep, Mute, Stop Listening & Wake Up ---
    sleep_phrases = [
        "shut up", "shut the fuck up", "stop listening", "go to sleep",
        "be quiet", "turn off mic", "turn off the mic", "turn your mic off",
        "stop the mic", "stay in idle", "go to idle", "stay idle",
        "mute mic", "mute", "quiet", "sleep"
    ]
    if any(p == t or t.startswith(p) for p in sleep_phrases) or any(p in t for p in ["shut up", "shut the fuck up", "stop listening", "go to sleep", "be quiet"]):
        return {"action": "sleep"}

    wake_phrases = [
        "wake up", "wake up jarvis", "listen jarvis", "start listening",
        "are you there jarvis", "jarvis wake up"
    ]
    if any(p == t or t.startswith(p) for p in wake_phrases):
        return {"action": "wake"}

    # --- 1. Music & Spotify controls (Checked FIRST to avoid false app closure) ---
    pause_phrases = [
        "pause spotify", "pause the song", "pause song", "stop the music",
        "stop music", "stop the song", "stop song", "pause music", "pause the music",
        "stop playing", "pause the playback", "stop playback", "stop spotify",
        "hey jarvis stop the spotify", "stop the spotify", "pause playback",
        "pause the track", "stop the track", "pause"
    ]
    if any(p in t for p in pause_phrases) or t in ("pause", "stop the song", "stop the music", "pause music", "stop song"):
        return {"action": "spotify", "target": "pause"}

    resume_phrases = [
        "resume spotify", "resume song", "resume the song", "resume music",
        "resume the music", "continue music", "unpause", "play music",
        "resume playback", "continue playing", "unpause spotify", "unpause music"
    ]
    if any(p in t for p in resume_phrases):
        return {"action": "spotify", "target": "resume"}

    track_nav_phrases = {
        "next": ["next song", "skip song", "next track", "skip track", "skip this song", "next track please"],
        "previous": ["previous song", "prev track", "previous track", "last song", "back song"]
    }
    for nav_cmd, phrases in track_nav_phrases.items():
        if any(p in t for p in phrases):
            return {"action": "spotify", "target": nav_cmd}

    if "spotify" in t and ("play" in t or "open" in t):
        if "open" in t and "play" not in t:
            return {"action": "open_app", "target": "Spotify"}
        # Extract song or playlist
        m = re.search(r'play\s+(?:some\s+|the\s+)?(?:music|song|playlist|track)?\s*(?:called\s+|named\s+)?(.*)', t)
        song = m.group(1).strip() if m else ""
        song = re.sub(r'\b(on\s+spotify|in\s+spotify)\b', '', song).strip()
        if song and song not in ("music", "songs", "some music", "spotify", "on spotify", "the spotify", "a song", "the song", "song", "play"):
            return {"action": "spotify", "target": f"play ||| {song}"}
        return {"action": "spotify", "target": "play"}

    # --- 2. Research Queries & Research Recall ---
    research_recall_phrases = [
        "where is my research", "where's my research", "give me my research", "show my research",
        "open my research", "show me my research", "what did the research find",
        "explain me the entire research", "read out loud and the entire research",
        "read out loud the entire research", "read out the research", "read the research",
        "tell me the entire research", "explain the research"
    ]
    if any(p in t for p in research_recall_phrases):
        return {"action": "show_research"}

    # --- 3. Live Tab & Screen Awareness ---
    tab_inspection_phrases = [
        "what's on my browser", "whats on my browser", "read this tab", "inspect this tab",
        "read this page", "summarize this page", "summarize this tab", "what's on this website",
        "use the tab that i you have open", "use the tab that you have open", "use the open tab",
        "calculate distance from the open tab", "use that tab to calculate", "what is on the google tab",
        "reading the tab", "read the tab", "find deal from tab", "find a best deal from me by reading the tab",
        "look at this tab", "read this"
    ]
    if any(p in t for p in tab_inspection_phrases):
        return {"action": "inspect_tab"}

    # --- 4. Flights & Airline Ticket Search ---
    flight_phrases = ["flight", "flights", "book a ticket", "book ticket", "airline ticket", "available tickets", "tickets to", "flight nearby me", "flights nearby"]
    if any(k in t for k in flight_phrases):
        m_d = re.search(r'(?:to|towards)\s+([a-zA-Z0-9_\s,]+)', t)
        dest = m_d.group(1).strip() if m_d else "Dubai"
        dest = re.sub(r'^(?:the\s+)', '', dest).strip()
        dest = re.sub(r'\s+(?:from\s+my\s+current\s+location|from\s+here|of\s+flight|flight|tickets?|on\s+that\s+day).*$', '', dest).strip()
        if not dest or dest in ("my", "here", "the", "that", "that flight", "all that flight"):
            dest = "Dubai"
        return {"action": "flights", "target": f"{dest} from Himmatnagar, Gujarat"}

    # Meta query check (e.g. "search is completed")
    if any(t == p or t.startswith(p) for p in ["search is completed", "search is complete", "is completed", "your search about the flight that is completed"]):
        return {"action": "flights", "target": "Dubai from Himmatnagar, Gujarat"}

    # --- 4. Maps, Route Planning & Travel Time Calculations ---
    # e.g. "from Himmatnagar to Ahmedabad how much time it takes", "plan trip to Ahmedabad", "how much time it took to reach me to the Ahmedabad"
    if any(k in t for k in ["map", "maps", "trip", "directions", "route", "how much time", "how long", "travel time", "distance to", "reach me to", "reach to", "reach"]):
        # Check "from X to Y"
        m_from_to = re.search(r'from\s+([a-zA-Z0-9_\s,]+?)\s+(?:to|towards)\s+([a-zA-Z0-9_\s,]+?)(?:\s+(?:how\s+much\s+time|how\s+long|calculate|distance|duration|in\s+the\s+car|right\s+now).*|$)', t)
        if m_from_to:
            orig = m_from_to.group(1).strip()
            dest = m_from_to.group(2).strip()
            b_name = "brave" if "brave" in t else ("apple" if "apple" in t else "chrome")
            return {"action": "maps", "target": f"from {orig} to {dest} in {b_name}"}

        # Check explicit reference like "open that in Google map on brave"
        if ("map" in t or "maps" in t) and re.search(r'\bopen\s+(?:that|it|this)\s*(?:in|on)\s+(?:google\s+|apple\s+)?maps?\b', t):
            b_name = "brave" if "brave" in t else ("apple" if "apple" in t else "chrome")
            return {"action": "maps", "target": f"current route in {b_name}"}

        # Check general route query or destination
        m_dest = re.search(r'(?:(?:open\s+(?:google\s+|apple\s+)?maps?\s+(?:and\s+)?)|(?:plan\s+(?:my\s+|a\s+)?trip\s+(?:for\s+me\s+)?(?:to\s+|for\s+)?)|(?:(?:how\s+much\s+time|how\s+long)\s+.*?(?:to\s+reach\s+(?:me\s+to\s+)?(?:the\s+)?|to\s+))|(?:to\s+reach\s+(?:me\s+to\s+)?(?:the\s+)?)|(?:(?:directions|route)\s+(?:to\s+|for\s+)?))([a-zA-Z0-9_\s,]+)', t)
        if m_dest:
            dest = m_dest.group(1).strip()
            dest = re.sub(r'^(?:plan\s+(?:a\s+|my\s+)?trip\s+(?:for\s+me\s+)?(?:to\s+)?|to\s+|for\s+|the\s+|me\s+to\s+)+', '', dest).strip()
            dest = re.sub(r'\s+(?:in|on)\s+(?:google\s+maps|apple\s+maps|chrome|brave|safari).*$', '', dest).strip()
            if dest and dest not in ("maps", "google maps", "apple maps", "that", "there", "it"):
                b_name = "brave" if "brave" in t else ("apple" if "apple" in t else "chrome")
                return {"action": "maps", "target": f"{dest} in {b_name}"}

    # Direct Google search or "can you use google"
    if t in ("can you use google", "use google", "open google", "open google in browser", "search on google", "google it") or re.match(r'^(?:can\s+you\s+)?(?:use|open)\s+google(?:\.com)?(?:\s+(?:in|on)\s+(?:browser|chrome|brave|safari))?$', t):
        return {"action": "browse", "target": "https://www.google.com"}

    # WhatsApp search & messaging
    if ("whatsapp" in t or "whats app" in t) and not any(t.startswith(w) for w in ["close", "quit", "kill", "exit", "shut down", "stop"]):
        m_search = re.search(r'(?:search|find|look\s*up)\s+(?:for\s+)?([a-zA-Z0-9_\s]+?)\s+(?:on|in)\s+whatsapp', t)
        if m_search:
            return {"action": "whatsapp", "target": m_search.group(1).strip()}
        has_open_verb = re.search(r'\b(open|launch|start|message|chat|send|text)\b', t)
        if has_open_verb:
            m = re.search(r'(?:to|with|message|chat with|send to|text)\s+([a-zA-Z0-9_\s]+)', t)
            contact = m.group(1).strip() if m else ""
            contact = re.sub(r'\b(on\s+whatsapp|in\s+whatsapp)\b', '', contact).strip()
            return {"action": "whatsapp", "target": contact}

    # Live Weather & Rain/Umbrella check
    weather_phrases = [
        "what is the weather", "whats the weather", "what's the weather", "how is the weather",
        "current weather", "weather today", "weather forecast", "weather in my current location",
        "weather outside", "exact weather", "is it raining", "is there raining", "raining or sunny",
        "carry my umbrella", "need an umbrella", "need umbrella", "umbrella today", "umbrella outside"
    ]
    if any(p in t for p in weather_phrases):
        return {"action": "weather", "target": t}

    # Product / Katana / Job Search queries
    if any(k in t for k in ["katana", "demon slayer", "tengen uzui", "buy katana", "shop katana", "job", "jobs", "hiring", "role"]):
        clean_q = t
        clean_q = re.sub(r'^(?:i\s+want\s+to\s+shop\s+(?:up\s+)?|find\s+a\s+perfect\s+vendor\s+for\s+me\s+or\s+you\s+can\s+search\s+that\s+on\s+browser\s+as\s+well\s+|find\s+a\s+perfect\s+job\s+for\s+my\s+role\s+my\s+role\s+is\s+|search\s+for\s+that\s+|bring\s+me\s+the\s+right\s+store\s+to\s+buy\s+a\s+)', '', clean_q, flags=re.I).strip()
        if "katana" in clean_q or "demon slayer" in clean_q or "tengen uzui" in clean_q:
            return {"action": "browse", "target": f"https://www.google.com/search?q={urllib.parse.quote('buy Tengen Uzui Nichirin Cleavers Katana Demon Slayer replica authentic')}"}
        if "job" in t or "role" in t or "engineer" in t or "ai" in t:
            return {"action": "browse", "target": f"https://www.google.com/search?q={urllib.parse.quote('AI ML engineer jobs Ahmedabad remote')}"}

    # Specific search on named browser (e.g. "search Abrar akunji in brave browser")
    m_sb = re.search(r'^(?:search|look\s*up|google)\s+(?:for\s+)?(.+?)\s+(?:on|in)\s+(brave|safari|chrome|google chrome|firefox|edge|arc)(?:\s+browser)?$', t)
    if m_sb:
        return {"action": "browse", "target": f"{m_sb.group(1).strip()} in {m_sb.group(2).strip()}"}

    # Open web service directly (e.g. Google Meet, YouTube, etc.)
    m_ws = re.search(r'^(?:open|launch|start|go\s*to)\s+(google\s+meet|meet|google\s+meeting|youtube|gmail|github|chatgpt|claude|twitter|reddit|opsmentum(?:\.com)?)\s*(?:on|in)?\s*(?:the\s+)?(?:browser|chrome|safari|brave|firefox|edge|arc)?$', t)
    if m_ws:
        svc = m_ws.group(1).strip()
        m_b = re.search(r'(?:on|in)\s+(?:the\s+)?(brave|safari|chrome|firefox|edge|arc)', t)
        b_name = m_b.group(1).strip() if m_b else "chrome"
        if "opsmentum" in svc:
            return {"action": "browse", "target": f"https://opsmentum.com in {b_name}"}
        return {"action": "browse", "target": f"{svc} in {b_name}"}

    browse_patterns = [
        r'^(?:now\s+)?open\s+(safari|chrome|google chrome|brave|firefox|edge|arc)(?:\s+browser)?\s+(?:and\s+)?(?:do\s+(?:some\s+|a\s+|deep\s+)?)?(?:research|search|look\s*up|google)\s+(?:about\s+|on\s+|for\s+)?(.+)$',
        r'^(?:do\s+(?:some\s+)?)?(?:search|look\s*up|google)\s+(?:about\s+|on\s+|for\s+)?(.+)$',
        r'^(?:browse|go\s*to)\s+(.+)$'
    ]
    for pattern in browse_patterns:
        bm = re.match(pattern, t)
        if bm:
            if len(bm.groups()) == 2:
                browser, query = bm.group(1), bm.group(2)
                return {"action": "browse", "target": f"{query.strip()} in {browser.strip()}"}
            else:
                query = bm.group(1)
                return {"action": "browse", "target": query.strip()}

    # Explicit deep research trigger phrases
    research_match = re.search(
        r'(?:(?:today\s+)?(?:i\s+am\s+giving\s+you\s+a\s+task\s+)?(?:you\s+need\s+to\s+|i\s+want\s+(?:you\s+)?to\s+|please\s+|can\s+you\s+)?(?:do\s+(?:some\s+|a\s+|deep\s+|more\s+|crazy\s+and\s+deep\s+|detailed\s+)?)?research\s+(?:about|on|for|into)?\s*(.+)|(?:tell\s+me|find|give\s+me)\s+(?:all\s+)?(?:the\s+)?(?:details|information)\s+(?:about|on)\s+(.+))',
        t
    )
    if research_match:
        topic = (research_match.group(1) or research_match.group(2) or "").strip()
        topic = re.sub(r'^(about|on|for|into)\s+', '', topic, flags=re.I).strip()
        topic = re.sub(r'\s*(?:and\s+)?(?:give|show|send)\s+me\s+(?:all\s+)?(?:the\s+)?results\s+(?:in|to)\s+(?:the\s+)?(?:chat|browser|screen)?.*$', '', topic, flags=re.I).strip()
        if topic and len(topic) > 1 and not any(topic.startswith(b) for b in ["you", "your", "what"]):
            return {"action": "research", "target": topic}

    # --- 5. Calendar Meeting / Scheduling ---
    if any(k in t for k in ("schedule", "book a meeting", "set up a meeting", "schedule a meeting", "add event", "create meeting", "schedule a call")):
        m_time = re.search(r'(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)?)', t)
        time_str = m_time.group(1) if m_time else "3:00 PM"
        title = "Meeting on Google Meet" if ("google meet" in t or "meet" in t) else ("Scheduled Meeting" if "meeting" in t or "call" in t else "Calendar Event")
        return {"action": "schedule", "target": f"{title} ||| {time_str}"}

    # --- 6. Close / Quit detection ---
    if any(w in t for w in ["close opencode", "close open code", "quit opencode", "quit open code", "stop opencode"]):
        return {"action": "close_app", "target": "Terminal"}

    close_match = re.match(
        r'^(?:close|quit|kill|exit|shut\s*down|stop|end)\s+(?:the\s+)?(.+?)(?:\s+app(?:lication)?)?$', t
    )
    if close_match:
        app_raw = close_match.group(1).strip()
        # Don't match media words or generic system phrases
        non_app_words = ("it", "this", "that", "everything", "all", "the", "my", "song", "music", "playback", "track", "playing", "songs")
        if app_raw not in non_app_words:
            from actions import APP_ALIASES
            normalized = app_raw.replace(" ", "").lower()
            spaced = app_raw.lower()
            resolved = APP_ALIASES.get(spaced) or APP_ALIASES.get(normalized)
            if not resolved:
                for alias_key, alias_val in APP_ALIASES.items():
                    if normalized == alias_key.replace(" ", "") or spaced == alias_key:
                        resolved = alias_val
                        break
            if resolved:
                return {"action": "close_app", "target": resolved}
            return {"action": "close_app", "target": app_raw.title() if len(app_raw.split()) == 1 else app_raw}

    # --- 7. Screen requests ---
    if any(p in t for p in ["look at my screen", "what's on my screen", "whats on my screen",
                             "what am i looking at", "what do you see", "see my screen",
                             "what's running on my", "whats running on my", "check my screen",
                             "what's open", "whats open", "what apps are open"]):
        return {"action": "describe_screen"}

    # --- 8. Terminal / coding workspace ---
    if any(w in t for w in ["open claude", "start claude", "launch claude", "run claude",
                             "open coding workspace", "open opencode", "open open code",
                             "start opencode", "launch opencode", "run opencode", "open ollama"]):
        return {"action": "open_terminal"}

    # --- 9. Show recent build ---
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # --- 10. Calendar check ---
    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting", "check my calendar"]):
        return {"action": "check_calendar"}

    # --- 11. Mail check ---
    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}

    # --- 12. Dispatch / build status ---
    if any(p in t for p in ["where are we", "where were we", "project status", "how's the build",
                             "hows the build", "status update", "status report", "where is that",
                             "how's it going with", "hows it going with", "is it done",
                             "is that done", "what happened with"]):
        return {"action": "check_dispatch"}

    # --- 12. WhatsApp (only with explicit verb) ---
    if ("whatsapp" in t or "whats app" in t):
        has_open_verb = re.search(r'\b(open|launch|start|message|chat|send|text)\b', t)
        if has_open_verb:
            m = re.search(r'(?:to|with|message|chat with|send to|text)\s+([a-zA-Z0-9_\s]+)', t)
            contact = m.group(1).strip() if m else ""
            contact = re.sub(r'\b(on\s+whatsapp|in\s+whatsapp)\b', '', contact).strip()
            return {"action": "whatsapp", "target": contact}
        return None

    # --- 13. Open folder / directory ---
    if "folder" in t or "directory" in t:
        m = re.search(r'open\s+(?:the\s+)?(?:folder|directory)\s+([a-zA-Z0-9_\-\s]+)', t)
        if not m:
            m = re.search(r'open\s+(?:the\s+)?([a-zA-Z0-9_\-\s]+?)\s+(?:folder|directory)', t)
        folder = m.group(1).strip() if m else ""
        if folder:
            return {"action": "open_folder", "target": folder}

    # --- 14. General App Opening (requires explicit "open/launch/start" verb) ---
    open_match = re.match(r'^(?:open|launch|start)\s+(?:the\s+|up\s+)?(.+?)(?:\s+app(?:lication)?)?$', t)
    if open_match:
        app_raw = open_match.group(1).strip()
        skip_phrases = ["terminal", "claude", "coding workspace", "opencode", "ollama",
                        "chrome and", "browser and", "safari and"]
        if any(app_raw.startswith(s) for s in skip_phrases):
            pass
        else:
            from actions import APP_ALIASES
            normalized = app_raw.replace(" ", "").lower()
            spaced = app_raw.lower()
            resolved = APP_ALIASES.get(spaced) or APP_ALIASES.get(normalized)
            if not resolved:
                for alias_key, alias_val in APP_ALIASES.items():
                    if normalized == alias_key.replace(" ", "") or spaced == alias_key:
                        resolved = alias_val
                        break
            if resolved:
                return {"action": "open_app", "target": resolved}
            if len(app_raw) > 1 and not any(c in app_raw for c in ["?", "!", "."]):
                return {"action": "open_app", "target": app_raw.title()}

    return None  # Everything else goes to the LLM for conversational routing


# -- Action Handlers -------------------------------------------------------

async def handle_open_terminal() -> str:
    engine = select_default_engine()
    if engine == "opencode":
        result = await open_terminal("opencode .")
    elif engine == "ollama":
        result = await open_terminal(f"ollama run {os.getenv('OLLAMA_MODEL', 'qwen2.5-coder:14b')}")
    else:
        return "No coding workspace engine is installed, sir."
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)
    result = await open_claude_in_project(path, build_task_brief(target))
    recently_built.append({"name": name, "path": path, "time": time.time()})
    if result.get("success"):
        return f"On it, sir. The coding workspace is working in {name}."
    return result.get("confirmation", "Had trouble starting the coding workspace, sir.")


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    # Try to find the best file to open
    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}, sir."

    # Try any HTML file
    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    # Fall back to opening the folder in Finder
    script = f'tell application "Finder"\nactivate\nopen POSIX file "{last["path"]}"\nend tell'
    await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return f"Opened the {last['name']} folder in Finder, sir."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


async def _lookup_and_report(lookup_type: str, lookup_fn, ws, history: list[dict] = None, voice_state: dict = None):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_start_time = time.time()
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": lookup_start_time,
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=30,
        )

        _active_lookups[lookup_id]["status"] = "done"

        # Speak the result — only skip audio if user spoke AFTER this lookup started and within 3s
        user_last_spoke = voice_state.get("last_user_time", 0.0) if voice_state else 0.0
        if user_last_spoke > lookup_start_time and (time.time() - user_last_spoke < 3):
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke during lookup")
            # Result is still stored in history below
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts)
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": tts})
                else:
                    await ws.send_json({"type": "text", "text": tts})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}"
                for m in top
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment, sir."


async def _do_screen_lookup() -> str:
    """Screen describe — runs in thread."""
    return await describe_screen(brain)


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] in ("working", "thinking", "researching")]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        if lookup.get("type") == "research":
            topic = lookup.get("topic", "requested topic")
            parts.append(f"deep research into {topic} ({elapsed}s)")
        else:
            parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender


async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "chrome"
    for b in ("brave", "safari", "firefox", "edge", "arc"):
        if b in text.lower():
            browser = b
            break

    # 1. Try to find a URL or domain in the text
    url_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)'
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    words = text.split()
    for i, word in enumerate(words):
        if re.search(r'\.(com|co|io|ai|org|net|dev|app)$', word, re.IGNORECASE):
            domain = word
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to search
    query = target
    for prefix in ["search for", "look up", "google", "find me", "pull up", "open chrome",
                    "open firefox", "open browser", "go to", "can you", "in the browser",
                    "can you go to", "please"]:
        query = query.lower().replace(prefix, "").strip()
    query = re.sub(r'\b(can|you|the|in|to|a|an|for|me|my|please)\b', '', query).strip()
    query = re.sub(r'\s+', ' ', query).strip()

def resolve_research_topic(raw_target: str) -> str:
    """Clean and resolve research topic from target string and chat history."""
    cleaned_target = (raw_target or "").strip()
    cleaned_target = re.sub(r'^(about|on|for|into)\s+', '', cleaned_target, flags=re.I).strip()
    cleaned_target = re.sub(r'\s*(?:and\s+)?(?:give|show|send)\s+me\s+(?:all\s+)?(?:the\s+)?results\s+(?:in|to)\s+(?:the\s+)?(?:chat|browser|screen)?.*$', '', cleaned_target, flags=re.I).strip()

    pronoun_phrases = ("that", "this", "it", "that information", "that topic", "the same", "the same thing", "that thing", "that and give me that information")
    if not cleaned_target or cleaned_target.lower() in pronoun_phrases or cleaned_target.lower().startswith("that and ") or len(cleaned_target) <= 4:
        for msg in _chat_history:
            if msg.get("role") == "user" and len(msg.get("text", "")) > 5:
                prev_text = msg["text"]
                if not any(prev_text.lower().startswith(w) for w in ["research", "search", "shut up", "wake", "pause", "go to sleep"]):
                    cleaned_target = prev_text
                    break

    if not cleaned_target:
        cleaned_target = "current artificial intelligence advancements"
    return cleaned_target


async def _research_and_report(text: str, cleaned_target: str, ws: WebSocket | None = None, history: list[dict] = None, voice_state: dict = None):
    """Execute deep research asynchronously, generate dark-mode HTML, open browser, post to chat, and speak executive summary."""
    lookup_start_time = time.time()
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "topic": cleaned_target,
        "type": "research",
        "status": "thinking",
        "started": lookup_start_time,
    }

    try:
        # Dedicated Thinking & Research Model: Qwen 2.5 / Qwen Think or Gemini
        if USE_LOCAL_BRAIN or brain.fast_brain.startswith("ollama/"):
            research_model = "ollama/jarvis-qwen-think"
        else:
            research_model = brain.eyes_brain if (GEMINI_API_KEY and not brain.eyes_brain.startswith("ollama/")) else DEFAULT_CHAT_MODEL

        log.info(f"Conducting deep research on '{cleaned_target}' via {research_model}")

        # Send thinking status and chat event immediately
        if ws:
            try:
                await ws.send_json({"type": "status", "state": "thinking", "text": f"Deep research into {cleaned_target}..."})
                await ws.send_json({
                    "type": "chat_event",
                    "message": {
                        "id": f"msg_{int(time.time() * 1000)}",
                        "role": "jarvis",
                        "text": f"🔬 **Deep Research & Thinking**: Investigating *{cleaned_target}* using {research_model.replace('ollama/', '')}...",
                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                        "model": research_model.replace("ollama/", ""),
                    }
                })
            except Exception:
                pass

        # 1. Fetch live web search snippets for real-time facts & grounding
        live_web_context = ""
        try:
            from browser import JarvisBrowser
            jb = JarvisBrowser()
            web_results = await jb.search(cleaned_target)
            if web_results:
                live_snippets = [f"- {r.title}: {r.snippet} (Source: {r.url})" for r in web_results[:5]]
                live_web_context = "\n".join(live_snippets)
                log.info(f"Retrieved {len(web_results)} live web search results for research on '{cleaned_target}'")
        except Exception as e:
            log.warning(f"Live web search pre-fetch failed: {e}")

        system_prompt = (
            f"You are JARVIS, an elite AI assistant researching for {USER_NAME}. "
            "Conduct a comprehensive, structured, and deeply analytical investigation on the requested topic. "
            "Include clear headings (# Title, ## Executive Summary, ## Key Insights, ## Detailed Analysis, ## Actionable Recommendations). "
            "Use clean Markdown with bullet points, bold key concepts, and cite specific real-world model names, companies, benchmarks, and dates."
        )

        user_prompt = f"Conduct exhaustive, factually accurate research on:\n\n{cleaned_target}"
        if live_web_context:
            user_prompt += f"\n\nLIVE SEARCH GROUNDING & WEB SOURCES:\n{live_web_context}\nIncorporate these real-world findings, named models, and facts."

        try:
            research_response = await brain.generate(
                model=research_model,
                max_tokens=1500,
                timeout=25 if research_model.startswith("ollama/") else 60,
                preserve_full_markdown=True,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            research_text = research_response.choices[0].message.content.strip()
        except Exception as err:
            log.warning(f"Primary research model {research_model} failed ({err}); failing over to cloud brain")
            failover_model = brain.eyes_brain if GEMINI_API_KEY else DEFAULT_CHAT_MODEL
            research_response = await brain.generate(
                model=failover_model,
                max_tokens=1500,
                timeout=45,
                preserve_full_markdown=True,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            research_text = research_response.choices[0].message.content.strip()

        # Format rich HTML report
        import html as _html
        escaped_body = _html.escape(research_text)
        formatted_body = re.sub(r'^###\s+(.+)$', r'<h3>\1</h3>', escaped_body, flags=re.M)
        formatted_body = re.sub(r'^##\s+(.+)$', r'<h2>\1</h2>', formatted_body, flags=re.M)
        formatted_body = re.sub(r'^#\s+(.+)$', r'<h1>\1</h1>', formatted_body, flags=re.M)
        formatted_body = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', formatted_body)
        formatted_body = re.sub(r'^\*\s+(.+)$', r'<li>\1</li>', formatted_body, flags=re.M)
        formatted_body = re.sub(r'^-\s+(.+)$', r'<li>\1</li>', formatted_body, flags=re.M)
        formatted_body = formatted_body.replace('\n\n', '<p>').replace('\n', '<br>')

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JARVIS Intelligence Report: {_html.escape(cleaned_target[:60])}</title>
<style>
  :root {{
    --bg: #07090e;
    --card: #0d1117;
    --accent: #38bdf8;
    --text: #e2e8f0;
    --text-muted: #94a3b8;
    --border: #1e293b;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    max-width: 860px;
    margin: 40px auto;
    padding: 32px;
    line-height: 1.75;
  }}
  .header {{
    border-bottom: 2px solid var(--border);
    padding-bottom: 20px;
    margin-bottom: 30px;
  }}
  .badge {{
    display: inline-block;
    background: rgba(56, 189, 248, 0.15);
    color: var(--accent);
    padding: 4px 12px;
    border-radius: 9999px;
    font-size: 0.8em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 12px;
  }}
  h1 {{ color: #f8fafc; font-size: 1.8em; margin: 8px 0; }}
  h2 {{ color: var(--accent); font-size: 1.25em; margin-top: 32px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  h3 {{ color: #7dd3fc; font-size: 1.05em; margin-top: 20px; }}
  p, li {{ color: #cbd5e1; font-size: 1em; }}
  li {{ margin-bottom: 6px; }}
  strong {{ color: #ffffff; }}
  .footer {{
    margin-top: 50px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--text-muted);
    font-size: 0.85em;
    display: flex;
    justify-content: space-between;
  }}
</style>
</head>
<body>
  <div class="header">
    <div class="badge">JARVIS Intelligence System</div>
    <h1>{_html.escape(cleaned_target)}</h1>
  </div>
  <div class="content">
    {formatted_body}
  </div>
  <div class="footer">
    <span>Engine: {research_model.replace("ollama/", "")}</span>
    <span>{datetime.now().strftime('%B %d, %Y • %I:%M %p')}</span>
  </div>
</body>
</html>"""

        results_file = Path.home() / "Desktop" / "jarvis_research.html"
        results_file.write_text(html_content)

        # Detect browser preference
        browser_name = "chrome"
        for b in ("brave", "safari", "firefox", "edge", "arc"):
            if b in text.lower():
                browser_name = b
                break

        # Open in browser directly
        await open_browser(f"file://{results_file}", browser_name)
        try:
            await asyncio.create_subprocess_exec("open", str(results_file))
        except Exception:
            pass

        # Post full research findings into Chat History & Chat Panel
        chat_msg = record_chat("jarvis", research_text, action={"action": "research", "target": cleaned_target}, model=research_model.replace("ollama/", ""))
        record_activity("model", f"Deep Research: {cleaned_target}", details=f"Generated using {research_model}")
        if ws:
            try:
                await ws.send_json({"type": "chat_event", "message": chat_msg})
            except Exception:
                pass

        # Generate concise 1-sentence executive summary with Gemma for speech
        summary = await brain.generate(
            model=brain.fast_brain,
            max_tokens=80,
            system="You are JARVIS. Summarize this research in ONE concise sentence for voice output. British butler tone, dry wit, economy of language.",
            messages=[{"role": "user", "content": research_text[:2000]}],
        )
        summary_text = summary.choices[0].message.content.strip().strip('"\'')

        global _last_research_record
        _last_research_record = {
            "topic": cleaned_target,
            "summary": summary_text,
            "html_file": str(results_file),
            "full_text": research_text,
            "time": time.time(),
        }

        _active_lookups[lookup_id]["status"] = "done"

        # Speak final result if user didn't speak during research
        final_msg = f"{summary_text} The complete findings are now open in your browser and posted to chat, sir."
        user_last_spoke = voice_state.get("last_user_time", 0.0) if voice_state else 0.0
        if ws:
            try:
                tts = strip_markdown_for_tts(final_msg)
                audio = await synthesize_speech(tts)
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": tts})
                else:
                    await ws.send_json({"type": "text", "text": tts})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception as e:
                log.warning(f"Failed sending research completion audio: {e}")

        if history is not None:
            history.append({"role": "assistant", "content": f"[Deep Research on {cleaned_target}]: {summary_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        if ws:
            try:
                fallback = f"The research into {cleaned_target} is taking longer than expected, sir. Still processing in the background."
                audio = await synthesize_speech(fallback)
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.error(f"Research failed: {e}")
        from urllib.parse import quote
        await open_browser(f"https://www.google.com/search?q={quote(cleaned_target)}")
    finally:
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def handle_research(text: str, target: str, ws: WebSocket | None = None) -> str:
    """Deep research with Qwen Think / Gemini / Groq — writes rich HTML, opens in browser, posts to chat."""
    cleaned_target = resolve_research_topic(target)
    if ws:
        asyncio.create_task(_research_and_report(text, cleaned_target, ws=ws))
        return f"Initiating deep research into {cleaned_target}, sir. Thinking through the details now; please allow me a moment."

    # Direct synchronous fallback when called without WebSocket
    try:
        if USE_LOCAL_BRAIN or brain.fast_brain.startswith("ollama/"):
            research_model = "ollama/jarvis-qwen-think"
        else:
            research_model = brain.eyes_brain if (GEMINI_API_KEY and not brain.eyes_brain.startswith("ollama/")) else DEFAULT_CHAT_MODEL

        system_prompt = (
            f"You are JARVIS, an elite AI assistant researching for {USER_NAME}. "
            "Conduct a comprehensive, structured, and deeply analytical investigation on the requested topic. "
            "Include clear headings (# Title, ## Executive Summary, ## Key Insights, ## Detailed Analysis, ## Actionable Recommendations). "
            "Use clean Markdown with bullet points, bold key concepts, and cite relevant facts and sources."
        )

        research_response = await brain.generate(
            model=research_model,
            max_tokens=1500,
            timeout=120,
            preserve_full_markdown=True,
            system=system_prompt,
            messages=[{"role": "user", "content": f"Conduct exhaustive research on:\n\n{cleaned_target}"}],
        )
        research_text = research_response.choices[0].message.content.strip()

        summary = await brain.generate(
            model=brain.fast_brain,
            max_tokens=80,
            system="You are JARVIS. Summarize this research in ONE concise sentence for voice output. British butler tone, dry wit, economy of language.",
            messages=[{"role": "user", "content": research_text[:2000]}],
        )
        summary_text = summary.choices[0].message.content.strip().strip('"\'')
        return f"{summary_text} Full report opened in your browser and posted to chat, sir."

    except Exception as e:
        log.error(f"Research fallback failed: {e}")
        return f"Research into {cleaned_target} ran into an error, sir."


async def handle_show_research(ws: WebSocket | None = None) -> str:
    """Re-open the last research document in browser and show in chat."""
    global _last_research_record
    if not _last_research_record:
        return "I haven't conducted any research yet, sir."
    results_file = _last_research_record["html_file"]
    await open_browser(f"file://{results_file}")
    try:
        await asyncio.create_subprocess_exec("open", str(results_file))
    except Exception:
        pass
    if ws and _last_research_record.get("full_text"):
        chat_msg = record_chat("jarvis", _last_research_record["full_text"])
        try:
            await ws.send_json({"type": "chat_event", "message": chat_msg})
        except Exception:
            pass
    return f"Displaying research on {_last_research_record['topic']} in your browser and chat, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await brain.generate(
            model=brain.butler_brain, # Use Intellectual Butler for summaries
            max_tokens=200,
            system=prompt,
            messages=[], # system prompt handles the instruction for now
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()
    
    # State for coding platform selection
    pending_coding_task: dict | None = None

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            greeting = "Good morning, sir."
        elif hour < 17:
            greeting = "Good afternoon, sir."
        else:
            greeting = "Good evening, sir."

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    audio_bytes = await synthesize_speech(greeting)
                    if audio_bytes and ws.client_state.name != "DISCONNECTED":
                        encoded = base64.b64encode(audio_bytes).decode()
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
                        history.append({"role": "assistant", "content": greeting})
                        log.info(f"JARVIS: {greeting}")
                        await ws.send_json({"type": "status", "state": "idle"})
                except Exception as e:
                    log.debug(f"Greeting failed (ignoring): {e}")

            asyncio.create_task(_send_greeting())

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg.get("type") == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = "Work mode active in my own repo, sir. Tell me what needs fixing."
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": audio, "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                continue

            # ── Sleep / Mute mode trigger ──
            if msg.get("type") == "sleep":
                log.info("Sleep signal received — entering dormant state")
                response_text = "Standing by, sir."
                tts = strip_markdown_for_tts(response_text)
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                continue

            # ── Wake mode trigger (via wake word or triple clap) ──
            if msg.get("type") == "wake":
                wake_src = msg.get("source", "voice")
                log.info(f"Wake signal received ({wake_src}) — resuming active listening")
                response_text = "I'm listening, sir."
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                await ws.send_json({"type": "status", "state": "listening"})
                continue

            if msg.get("type") != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(msg.get("text", "").strip())
            if not user_text:
                continue

            # Cancel any in-flight response
            _current_response_id += 1
            my_response_id = _current_response_id
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            start_turn_time = time.time()
            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")

            user_chat_msg = record_chat("user", user_text)
            record_activity("voice", f"User: {user_text}")
            try:
                await ws.send_json({"type": "chat_event", "message": user_chat_msg})
            except Exception:
                pass

            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync),
                        timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index:]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, ".jarvis_task.md").write_text(build_task_brief(prompt))
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif planner.active_plan and planner.active_plan.confirmed is False and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, ".jarvis_task.md").write_text(build_task_brief(prompt))
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan, sir?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
                        else:
                            response_text = result.get("next_question", "What else, sir?")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── CODING CHOICE: handling the choice between OpenCode and Ollama Cloud ──
                elif pending_coding_task:
                    choice = t_lower
                    task = pending_coding_task
                    pending_coding_task = None  # Reset state

                    from actions import launch_opencode, launch_ollama_workspace

                    if any(w in choice for w in ["opencode", "open code", "first one", "option 1"]):
                        log.info(f"User chose OpenCode for {task['name']}")
                        result = await launch_opencode(task["path"], task["target"])
                        response_text = result["confirmation"]
                        dispatch_registry.register(task["name"], task["path"], task["target"])
                    elif any(w in choice for w in ["ollama", "cloud", "second one", "option 2"]):
                        log.info(f"User chose Ollama for {task['name']}")
                        result = await launch_ollama_workspace(task["path"], task["target"])
                        response_text = result["confirmation"]
                        dispatch_registry.register(task["name"], task["path"], task["target"])
                    else:
                        pending_coding_task = task
                        response_text = "I'm sorry sir, I didn't quite catch that. Would you prefer OpenCode or Ollama?"

                # ── WORK MODE: speech → coding engine → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — bypass the coding engine, use the fast brain
                        response_text = await generate_response(
                            user_text, task_manager,
                            cached_projects, history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                        )
                    else:
                        # Send to the active coding engine
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → {work_session.engine_name}: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if the coding engine is stalling (asking questions instead of building)
                        if full_response:
                            stall_words = ["which option", "would you prefer", "would you like me to",
                                           "before I proceed", "before proceeding", "should I",
                                           "do you want me to", "let me know", "please confirm",
                                           "which approach", "what would you"]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling:
                                log.info("Coding engine is stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs the coding engine mentions
                        import re as _re
                        localhost_match = _re.search(r'https?://localhost:\d+', full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via the Brain
                        if full_response and brain:
                            try:
                                summary = await brain.generate(
                                    model=DEFAULT_CHAT_MODEL,
                                    max_tokens=100,
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                        "NEVER mention the coding engine by name. NEVER output [ACTION:...] tags. "
                                        "NEVER read out URLs. No markdown. British precision."
                                    ),
                                    messages=[{"role": "user", "content": f"Coding engine said:\n{full_response[:2000]}"}],
                                )
                                response_text = summary.choices[0].message.content
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    action = detect_action_fast(user_text)

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete, sir."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        elif action["action"] == "sleep":
                            response_text = "Standing by, sir."
                            try:
                                await ws.send_json({"type": "status", "state": "sleeping"})
                            except Exception:
                                pass
                        elif action["action"] == "wake":
                            response_text = "I'm listening, sir."
                            try:
                                await ws.send_json({"type": "status", "state": "listening"})
                            except Exception:
                                pass
                        elif action["action"] == "research":
                            raw_target = action.get("target", "")
                            cleaned_target = resolve_research_topic(raw_target)
                            response_text = f"Initiating deep research into {cleaned_target}, sir. Thinking through the details now; please give me a moment."
                            asyncio.create_task(_research_and_report(user_text, cleaned_target, ws=ws, history=history, voice_state=voice_state))
                        elif action["action"] == "show_research":
                            response_text = await handle_show_research(ws=ws)
                        elif action["action"] == "weather":
                            response_text = await fetch_live_weather(user_text)
                        elif action["action"] == "flights":
                            from actions import execute_action
                            res = await execute_action(action)
                            response_text = res.get("confirmation", "Searching for available flights, sir.")
                            md_card = res.get("markdown_card", "")
                            if ws and md_card:
                                try:
                                    await ws.send_json({
                                        "type": "chat_event",
                                        "message": {
                                            "id": f"msg_{int(time.time() * 1000)}",
                                            "role": "jarvis",
                                            "text": md_card,
                                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                                            "model": "jarvis/flights",
                                        }
                                    })
                                except Exception:
                                    pass
                        elif action["action"] == "maps":
                            from actions import execute_action
                            res = await execute_action(action)
                            response_text = res.get("confirmation", "Plotting your route in Maps, sir.")
                        elif action["action"] == "browse":
                            from actions import execute_action
                            res = await execute_action(action)
                            response_text = res.get("confirmation", "Searching in browser, sir.")
                        elif action["action"] in ("schedule", "schedule_event", "calendar_schedule"):
                            from actions import execute_action
                            res = await execute_action(action)
                            response_text = res.get("confirmation", "Scheduled on your calendar, sir.")
                        elif action["action"] == "open_app":
                            from actions import open_macos_app
                            res = await open_macos_app(action.get("target", ""))
                            response_text = res.get("confirmation", "Opening application, sir.")
                        elif action["action"] == "close_app":
                            from actions import close_macos_app
                            res = await close_macos_app(action.get("target", ""))
                            response_text = res.get("confirmation", "Closing application, sir.")
                        elif action["action"] == "spotify":
                            from actions import control_spotify
                            target = action.get("target", "play")
                            if "|||" in target:
                                cmd, _, q = target.partition("|||")
                                res = await control_spotify(cmd.strip(), q.strip())
                            elif target.lower() in ("pause", "stop", "resume", "unpause", "next", "skip", "previous", "prev"):
                                res = await control_spotify(target.lower(), "")
                            elif target.lower() in ("play", "music", "some music"):
                                res = await control_spotify("play", "")
                            else:
                                res = await control_spotify("play", target.strip())
                            response_text = res.get("confirmation", "Controlling Spotify, sir.")
                        elif action["action"] == "whatsapp":
                            from actions import open_whatsapp
                            target = action.get("target", "")
                            if "|||" in target:
                                c, _, m = target.partition("|||")
                                res = await open_whatsapp(c.strip(), m.strip())
                            else:
                                res = await open_whatsapp(target.strip())
                            response_text = res.get("confirmation", "Opening WhatsApp, sir.")
                        elif action["action"] == "inspect_tab":
                            from browser_vision import extract_google_maps_from_active_tab, extract_active_webpage_summary
                            maps_info = await extract_google_maps_from_active_tab()
                            if maps_info and (maps_info.get("duration") or maps_info.get("distance")):
                                dur = maps_info.get("duration", "")
                                dist = maps_info.get("distance", "")
                                road = maps_info.get("road", "")
                                response_text = f"According to your open Google Maps tab, the trip takes {dur} ({dist}) {road}, sir."
                            else:
                                tab_text = await extract_active_webpage_summary()
                                if tab_text and not tab_text.startswith("No active"):
                                    try:
                                        s_resp = await brain.generate(
                                            model=brain.fast_brain,
                                            max_tokens=100,
                                            system="Summarize what this web page shows in 1-2 concise sentences. British butler tone, address user as sir.",
                                            messages=[{"role": "user", "content": tab_text[:2000]}],
                                        )
                                        response_text = s_resp.choices[0].message.content.strip()
                                    except Exception:
                                        response_text = tab_text[:200]
                                else:
                                    response_text = "I'm monitoring your open browser tabs now, sir."
                        elif action["action"] == "open_folder":
                            from actions import open_local_folder
                            res = await open_local_folder(action.get("target", ""))
                            response_text = res.get("confirmation", "Opening folder in Finder, sir.")
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not brain.is_ready():
                            response_text = "My brain keys are not configured yet, sir."
                        else:
                            response_text = await generate_response(
                                user_text, task_manager,
                                cached_projects, history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = f"Connecting to {proj} now, sir."
                                    elif action_type == "build":
                                        response_text = "On it, sir."
                                    elif action_type == "research":
                                        response_text = "Looking into that now, sir."
                                    elif action_type == "open_app":
                                        response_text = f"Opening {embedded_action['target']}, sir."
                                    elif action_type == "close_app":
                                        response_text = f"Closing {embedded_action['target']}, sir."
                                    elif action_type == "spotify":
                                        response_text = "Controlling Spotify now, sir."
                                    elif action_type == "whatsapp":
                                        response_text = "Opening WhatsApp, sir."
                                    elif action_type == "open_folder":
                                        response_text = f"Opening {embedded_action['target']}, sir."
                                    elif action_type == "firecrawl":
                                        response_text = "Scraping with Firecrawl, sir."
                                    elif action_type in ("schedule", "calendar"):
                                        response_text = "Updating your calendar, sir."
                                    elif action_type == "browse":
                                        response_text = f"Opening that for you, sir."
                                    else:
                                        response_text = "Right away, sir."

                                if embedded_action["action"] == "open_app":
                                    from actions import open_macos_app
                                    asyncio.create_task(open_macos_app(embedded_action["target"]))
                                elif embedded_action["action"] == "close_app":
                                    from actions import close_macos_app
                                    asyncio.create_task(close_macos_app(embedded_action["target"]))
                                elif embedded_action["action"] in ("schedule", "schedule_event", "calendar_schedule"):
                                    from actions import execute_action
                                    asyncio.create_task(execute_action(embedded_action))
                                elif embedded_action["action"] == "spotify":
                                    from actions import control_spotify
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        cmd, _, q = target.partition("|||")
                                        asyncio.create_task(control_spotify(cmd.strip(), q.strip()))
                                    elif target.lower() in ("pause", "stop", "resume", "unpause", "next", "skip", "previous", "prev"):
                                        asyncio.create_task(control_spotify(target.lower(), ""))
                                    elif target.lower() in ("play", "music", "some music"):
                                        asyncio.create_task(control_spotify("play", ""))
                                    else:
                                        asyncio.create_task(control_spotify("play", target.strip()))
                                elif embedded_action["action"] == "whatsapp":
                                    from actions import open_whatsapp
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        contact, _, msg = target.partition("|||")
                                        asyncio.create_task(open_whatsapp(contact.strip(), msg.strip()))
                                    else:
                                        asyncio.create_task(open_whatsapp(target.strip()))
                                elif embedded_action["action"] == "open_folder":
                                    from actions import open_local_folder
                                    asyncio.create_task(open_local_folder(embedded_action["target"]))
                                elif embedded_action["action"] == "firecrawl":
                                    from actions import firecrawl_scrape
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        url, _, p = target.partition("|||")
                                        asyncio.create_task(firecrawl_scrape(url.strip(), p.strip()))
                                    else:
                                        asyncio.create_task(firecrawl_scrape(target.strip()))
                                elif embedded_action["action"] == "build":
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)

                                    engines = available_coding_engines()
                                    if engines["opencode"] and engines["ollama"]:
                                        pending_coding_task = {
                                            "target": build_task_brief(target),
                                            "name": name,
                                            "path": path,
                                        }
                                        response_text = "Ready to build, sir. Would you like me to use OpenCode or Ollama?"
                                    elif engines["opencode"]:
                                        from actions import launch_opencode
                                        result = await launch_opencode(path, build_task_brief(target))
                                        dispatch_registry.register(name, path, target)
                                        response_text = result["confirmation"]
                                    elif engines["ollama"]:
                                        from actions import launch_ollama_workspace
                                        result = await launch_ollama_workspace(path, build_task_brief(target))
                                        dispatch_registry.register(name, path, target)
                                        response_text = result["confirmation"]
                                    else:
                                        response_text = "No coding workspace engine is installed, sir."
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    # Research enters work mode too
                                    name = _generate_project_name(embedded_action["target"])
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)
                                    await work_session.start(path)
                                    asyncio.create_task(
                                        self_work_and_notify(work_session, embedded_action["target"], ws)
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                    else:
                                        # Forgiving parsing if ||| delimiter was omitted
                                        lines = [line.strip() for line in target.strip().split("\n") if line.strip()]
                                        proj_name = lines[0] if lines else "workspace"
                                        prompt = "\n".join(lines[1:]) if len(lines) > 1 else f"Review status of {proj_name}"

                                    recent = dispatch_registry.get_recent_for_project(proj_name)
                                    if recent and recent.get("summary"):
                                        log.info(f"Using recent dispatch result for {proj_name} instead of re-dispatching")
                                        response_text = recent["summary"]
                                        history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
                                    else:
                                        asyncio.create_task(
                                            _execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state)
                                        )
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(title=title, description=desc, priority=priority, due_date=due)
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        asyncio.create_task(create_apple_note("JARVIS Note", target))
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        note = await read_note(search_term)
                                        if note:
                                            msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        else:
                                            msg = f"Couldn't find a note matching '{search_term}', sir."
                                        audio = await synthesize_speech(strip_markdown_for_tts(msg))
                                        if audio and _ws:
                                            try:
                                                await _ws.send_json({"type": "status", "state": "speaking"})
                                                await _ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                            except Exception:
                                                pass
                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and brain:
                        summary_update_pending = True
                        messages_since_last_summary = 0
                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated
                            )
                            summary_update_pending = False
                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if brain and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, brain))

                # Calculate turn latency and record chat / activity
                turn_latency_ms = (time.time() - start_turn_time) * 1000
                used_action = action if "action" in locals() and action else (embedded_action if "embedded_action" in locals() and embedded_action else None)
                model_used = getattr(brain, "last_model_used", "ollama/jarvis-gemma")

                jarvis_chat_msg = record_chat(
                    role="jarvis",
                    text=response_text,
                    action=used_action,
                    model=model_used,
                    latency_ms=turn_latency_ms,
                )
                record_activity(
                    category="action" if used_action else "model",
                    title=f"JARVIS: {response_text[:80]}",
                    details=f"Model: {model_used} | Latency: {turn_latency_ms:.0f}ms",
                    latency_ms=turn_latency_ms,
                )
                try:
                    await ws.send_json({"type": "chat_event", "message": jarvis_chat_msg})
                except Exception:
                    pass

                # TTS
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
                    else:
                        await ws.send_json({"type": "audio", "data": "", "text": fallback})
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return Path(__file__).parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    provider: Optional[str] = None
    key_value: Optional[str] = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"

@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {
        "GROQ_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY", "FIRECRAWL_API_KEY",
        "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS",
        "OPENCODE_MODEL", "OLLAMA_MODEL", "OLLAMA_HOST", "USE_LOCAL_BRAIN",
        "DEFAULT_CHAT_MODEL", "PERSONALITY_MODEL", "FALLBACK_CHAT_MODEL", "ANALYTICAL_MODEL",
    }
    if body.key_name not in allowed:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    _write_env_key(body.key_name, body.key_value)
    return {"success": True}

@app.post("/api/settings/test-provider")
async def api_test_provider(body: KeyTest):
    provider = (body.provider or "").strip().lower()
    if provider == "ollama":
        try:
            model = body.key_value or os.getenv("OLLAMA_MODEL", "jarvis-qwen")
            if not model.startswith("ollama/"):
                model = f"ollama/{model}"
            await litellm.acompletion(
                model=model,
                api_base=OLLAMA_HOST,
                messages=[{"role": "user", "content": "Reply with: ok"}],
                max_tokens=8,
                timeout=15,
            )
            return {"valid": True}
        except Exception as exc:
            return {"valid": False, "error": str(exc)[:200]}

    if provider == "firecrawl":
        key = body.key_value or os.getenv("FIRECRAWL_API_KEY", "")
        if not key:
            return {"valid": False, "error": "No Firecrawl key provided"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get("https://api.firecrawl.dev/v1/scrape", headers={"Authorization": f"Bearer {key}"})
                if res.status_code != 401:
                    return {"valid": True}
                return {"valid": False, "error": "Invalid Firecrawl API key"}
        except Exception as exc:
            return {"valid": False, "error": str(exc)[:200]}

    provider_env = {
        "groq": "GROQ_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "nvidia": "NVIDIA_API_KEY",
    }
    if provider not in provider_env:
        return JSONResponse({"valid": False, "error": "Unsupported provider"}, status_code=400)

    key = body.key_value or os.getenv(provider_env[provider], "")
    if not key:
        return {"valid": False, "error": "No key provided"}

    try:
        model = PROVIDER_TEST_MODELS[provider]
        env_backup = os.environ.get(provider_env[provider])
        os.environ[provider_env[provider]] = key
        try:
            await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": "Reply with: ok"}],
                max_tokens=8,
                timeout=15,
            )
        finally:
            if env_backup is None:
                os.environ.pop(provider_env[provider], None)
            else:
                os.environ[provider_env[provider]] = env_backup
        return {"valid": True}
    except Exception as exc:
        return {"valid": False, "error": str(exc)[:200]}

@app.get("/api/settings/status")
async def api_settings_status():
    _, env_dict = _read_env()
    engines = available_coding_engines()
    def _is_app_installed(app_name: str) -> bool:
        for p in ("/System/Applications", "/Applications"):
            if (Path(p) / f"{app_name}.app").exists():
                return True
        return False

    calendar_ok = _is_app_installed("Calendar")
    mail_ok = _is_app_installed("Mail")
    notes_ok = _is_app_installed("Notes")
    memory_count = task_count = 0
    try: memory_count = len(get_important_memories(limit=9999))
    except Exception: pass
    try: task_count = len(get_open_tasks())
    except Exception: pass
    is_local = env_dict.get("USE_LOCAL_BRAIN", "").lower() in ("1", "true", "yes") or env_dict.get("DEFAULT_CHAT_MODEL", "").startswith("ollama/")
    return {
        "coding_engines": engines,
        "calendar_accessible": calendar_ok,
        "mail_accessible": mail_ok,
        "notes_accessible": notes_ok,
        "memory_count": memory_count,
        "task_count": task_count,
        "server_port": 8340,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "groq": _env_has_real_value(env_dict, "GROQ_API_KEY"),
            "gemini": _env_has_real_value(env_dict, "GEMINI_API_KEY"),
            "nvidia": _env_has_real_value(env_dict, "NVIDIA_API_KEY"),
            "firecrawl": _env_has_real_value(env_dict, "FIRECRAWL_API_KEY"),
            "ollama": bool(engines.get("ollama") or _env_has_real_value(env_dict, "OLLAMA_MODEL") or is_local),
            "use_local_brain": is_local,
            "user_name": env_dict.get("USER_NAME", ""),
        },
    }


@app.get("/api/system/activity")
async def api_system_activity():
    """Real-time system resource consumption (CPU, RAM, GPU, activities)."""
    load1, load5, load15 = os.getloadavg()
    cpu_count = os.cpu_count() or 8
    cpu_percent = min(100.0, round((load1 / cpu_count) * 100, 1))

    total_gb, used_gb, ram_percent = 16.0, 8.0, 50.0
    try:
        mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
        total_gb = round(mem_bytes / (1024**3), 1)
        vm = subprocess.check_output(["vm_stat"]).decode()
        page_size = 4096
        m_page = re.search(r"page size of (\d+) bytes", vm)
        if m_page:
            page_size = int(m_page.group(1))
        active_pages = int(re.search(r"Pages active:\s+(\d+)", vm).group(1))
        wired_pages = int(re.search(r"Pages wired down:\s+(\d+)", vm).group(1))
        compressed_pages = 0
        m_comp = re.search(r"Pages occupied by compressor:\s+(\d+)", vm)
        if m_comp:
            compressed_pages = int(m_comp.group(1))
        used_bytes = (active_pages + wired_pages + compressed_pages) * page_size
        used_gb = round(used_bytes / (1024**3), 1)
        ram_percent = min(100.0, round((used_bytes / mem_bytes) * 100, 1))
    except Exception:
        pass

    try:
        chip = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
    except Exception:
        chip = "Apple M4"

    return {
        "metrics": {
            "cpu": {
                "percent": cpu_percent,
                "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
                "cores": cpu_count,
            },
            "ram": {
                "percent": ram_percent,
                "used_gb": used_gb,
                "total_gb": total_gb,
            },
            "chip": chip,
            "gpu": {
                "name": f"{chip} Unified GPU",
                "status": "Nominal / Active",
                "memory": f"{total_gb} GB Unified UMA",
                "neural_engine": "Active (Apple Neural Engine 16-Core)",
            },
            "uptime_seconds": int(time.time() - _session_start),
            "active_tasks": len(task_manager.get_all_tasks()) if hasattr(task_manager, "get_all_tasks") else 0,
        },
        "activities": _activity_feed[:50],
    }


@app.get("/api/chat/history")
async def api_chat_history(session_id: Optional[str] = None):
    """Retrieve full formatted conversation log from persistent graph store."""
    try:
        from memory_graph import get_session_messages, get_or_create_active_session
        sid = session_id or get_or_create_active_session()
        messages = get_session_messages(sid)
        return {"messages": messages, "session_id": sid}
    except Exception:
        return {"messages": _chat_history}


@app.get("/api/chat/sessions")
async def api_chat_sessions():
    """List all saved conversation sessions."""
    from memory_graph import list_sessions
    return {"sessions": list_sessions()}


@app.post("/api/chat/sessions/new")
async def api_chat_new_session():
    """Start a brand new chat session."""
    from memory_graph import create_session, get_session_messages
    sid = create_session("New Conversation")
    global _chat_history
    _chat_history = get_session_messages(sid)
    return {"session_id": sid, "messages": _chat_history}


class SessionSwitchRequest(BaseModel):
    session_id: str


@app.post("/api/chat/sessions/switch")
async def api_chat_switch_session(body: SessionSwitchRequest):
    """Switch active conversation session."""
    from memory_graph import set_active_session, get_session_messages
    ok = set_active_session(body.session_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    global _chat_history
    _chat_history = get_session_messages(body.session_id)
    return {"session_id": body.session_id, "messages": _chat_history}


@app.get("/api/graph/stats")
async def api_graph_stats():
    """Return knowledge graph node/edge counts and active session."""
    from memory_graph import get_graph_stats
    return get_graph_stats()


class GraphQueryRequest(BaseModel):
    query: str


@app.post("/api/graph/query")
async def api_graph_query(body: GraphQueryRequest):
    """Query knowledge graph context."""
    from memory_graph import query_graph_context
    ctx = query_graph_context(body.query)
    return {"context": ctx}


class ChatSendMessage(BaseModel):
    text: str


@app.post("/api/chat/send")
async def api_chat_send(body: ChatSendMessage):
    """Process a typed command or question from the frontend chat panel."""
    user_text = apply_speech_corrections(body.text.strip())
    if not user_text:
        return {"error": "Empty text"}

    start_turn_time = time.time()
    user_chat_msg = record_chat("user", user_text)
    record_activity("text_input", f"User (Typed): {user_text}")

    action = detect_action_fast(user_text)
    used_action = None
    if action:
        used_action = action
        from actions import execute_action
        res = await execute_action(action)
        response_text = res.get("confirmation", "Understood, sir.")
    else:
        response_text = await generate_response(
            user_text, task_manager, cached_projects, [],
        )
        clean_resp, emb_action = extract_action(response_text)
        if emb_action:
            used_action = emb_action
            from actions import execute_action
            asyncio.create_task(execute_action(emb_action))
            response_text = clean_resp or "Right away, sir."

    turn_latency_ms = (time.time() - start_turn_time) * 1000
    model_used = getattr(brain, "last_model_used", "ollama/jarvis-gemma")
    jarvis_chat_msg = record_chat(
        role="jarvis",
        text=response_text,
        action=used_action,
        model=model_used,
        latency_ms=turn_latency_ms,
    )
    record_activity("model" if not used_action else "action", f"JARVIS: {response_text[:80]}", latency_ms=turn_latency_ms)

    return {
        "user_message": user_chat_msg,
        "jarvis_message": jarvis_chat_msg,
    }


@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
    }

@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    return {"success": True}

# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — shutting down in 2 seconds")
    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "0.0.0.0"]
        os.execv(sys.executable, cmd)
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    fix_prompt = build_task_brief("Inspect the JARVIS project and fix the most immediate issue that is blocking the product.")
    await open_claude_in_project(jarvis_dir, fix_prompt)
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )
