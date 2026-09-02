# JARVIS — Voice AI Assistant

## Overview
JARVIS (Just A Rather Very Intelligent System) is a voice-first AI assistant for macOS. It runs locally on your machine, connects to Apple Calendar, Mail, and Notes, and can open coding workspaces with OpenCode or Ollama for development tasks.

## Quick Start
When a user clones this repo and starts Codex, help them:
1. Copy `.env.example` to `.env`
2. Get a Groq API key from `console.groq.com`
3. Get a Gemini API key from Google AI Studio
4. Optionally add an NVIDIA API key for analytical/fallback routing
5. Install Python dependencies: `pip install -r requirements.txt`
6. Install frontend dependencies: `cd frontend && npm install`
7. Generate SSL certs if HTTPS is needed: `openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'`
8. Run the backend: `python server.py --port 8340`
9. Run the frontend: `cd frontend && npm run dev`
10. Open Chrome to `http://localhost:5173`
11. Click to enable audio, speak to JARVIS

## Architecture
- **Backend**: FastAPI + Python (`server.py`)
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb)
- **Communication**: WebSocket (JSON messages + binary audio)
- **AI Routing**: LiteLLM in-process
- **Fast Brain**: Groq
- **Reasoning / Vision**: Gemini
- **Analytical / Fallback**: NVIDIA (optional)
- **TTS**: Local macOS voice synthesis via `say` (captured to binary audio for orb reaction)
- **System**: AppleScript for Calendar, Mail, Notes, and Terminal integration
- **Coding Engines**: OpenCode and Ollama


## Key Files
- `server.py` — Main server, WebSocket handler, provider routing, action system
- `frontend/src/orb.ts` — Three.js particle orb visualization
- `frontend/src/voice.ts` — Web Speech API + audio playback
- `frontend/src/main.ts` — Frontend state machine
- `frontend/src/settings.ts` — Provider setup and system status panel
- `memory.py` — SQLite memory system with FTS5 search
- `calendar_access.py` — Apple Calendar integration via AppleScript
- `mail_access.py` — Apple Mail integration (READ-ONLY)
- `notes_access.py` — Apple Notes integration
- `actions.py` — System actions (Terminal, Chrome, OpenCode, Ollama)
- `browser.py` — Playwright web automation
- `work_mode.py` — Persistent coding-engine sessions

## Environment Variables
- `GROQ_API_KEY` (required) — Fast conversational routing
- `GEMINI_API_KEY` (required) — Reasoning and vision routing
- `NVIDIA_API_KEY` (optional) — Analytical/fallback routing
- `USER_NAME` (optional) — Your name for JARVIS to use
- `CALENDAR_ACCOUNTS` (optional) — Comma-separated calendar emails
- `OPENCODE_MODEL` (optional) — Override the OpenCode model
- `OLLAMA_MODEL` (optional) — Override the Ollama model
- `OLLAMA_HOST` (optional) — Remote Ollama host

## Conventions
- JARVIS personality: British butler, dry wit, economy of language
- Max 1-2 sentences per voice response
- Action tags: `[ACTION:BUILD]`, `[ACTION:BROWSE]`, `[ACTION:RESEARCH]`, etc.
- AppleScript for all macOS integrations
- Mail stays read-only
- SQLite for all local data storage
