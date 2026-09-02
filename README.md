# FixO

**Your Voice-First AI Companion & Autonomous Developer Workstation.**

FixO is a voice-first AI assistant for macOS. It listens through the browser, responds with speech, watches your calendar/mail/notes, and can open local coding workspaces with OpenCode or Ollama when you want development work done.

## What It Does

- Voice conversation with spoken responses
- Multi-provider AI routing with Groq, Gemini, and optional NVIDIA
- Local macOS voice synthesis with audio-reactive animations
- Apple Calendar, Mail, and Notes integration via AppleScript
- Project awareness and coding workspace orchestration
- Audio-reactive Three.js orb UI

## Requirements

- macOS
- Python 3.11+
- Node.js 18+
- Google Chrome
- Groq API key
- Gemini API key
- Optional NVIDIA API key
- Optional OpenCode CLI
- Optional Ollama CLI

## Quick Start

```bash
git clone https://github.com/abrar225/FixO.git
cd FixO
cp .env.example .env
```

Edit `.env` with at least:

```env
GROQ_API_KEY=your-groq-api-key-here
GEMINI_API_KEY=your-gemini-api-key-here
```

Optional:

```env
NVIDIA_API_KEY=your-nvidia-api-key-here
USER_NAME=Tony
CALENDAR_ACCOUNTS=you@gmail.com,work@company.com
OPENCODE_MODEL=openai/gpt-5.1-codex-mini
OLLAMA_MODEL=qwen2.5-coder:14b
OLLAMA_HOST=http://localhost:11434
```

Install dependencies and run:

```bash
pip install -r requirements.txt
cd frontend && npm install && cd ..
python server.py --port 8340
cd frontend && npm run dev
```

Open Chrome to [http://localhost:5173](http://localhost:5173), click once to enable audio, and speak.

## HTTPS / SSL

Development defaults to plain HTTP and `ws://`:

- Backend: `http://localhost:8340`
- Frontend: `http://localhost:5173`

If you want HTTPS/WSS locally, generate certificates:

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'
python server.py --port 8340 --ssl
```

## Architecture

```text
Microphone -> Web Speech API -> WebSocket -> FastAPI -> LiteLLM Router -> Models
                                                      |-> Groq (fast chat)
                                                      |-> Gemini (reasoning / vision)
                                                      |-> NVIDIA (optional analytics)
                                                      |
                                                      v
                                              macOS say (local synthesis)
                                                      |
                                                      v
                                       OpenCode / Ollama coding workspace
```

## Key Files

- `server.py` — main server, voice loop, provider routing, settings API
- `work_mode.py` — persistent coding-engine sessions
- `actions.py` — Terminal/browser/OpenCode/Ollama launch helpers
- `memory.py` — SQLite memory, tasks, notes
- `calendar_access.py` — Apple Calendar integration
- `mail_access.py` — Apple Mail integration
- `notes_access.py` — Apple Notes integration
- `frontend/src/main.ts` — frontend state machine
- `frontend/src/settings.ts` — provider setup and engine status UI
- `frontend/src/orb.ts` — Three.js orb
- `frontend/src/voice.ts` — speech recognition and audio playback

## Coding Engines

JARVIS supports two local development backends:

- **OpenCode** for persistent session-style coding work
- **Ollama** for local or remote model-backed coding work

When both are installed, JARVIS asks which one to use for new builds. If only one is installed, it uses that engine automatically.

## Troubleshooting

- If the settings panel keeps opening on launch, one of `GROQ_API_KEY` or `GEMINI_API_KEY` is still missing.
- If the frontend shows proxy connection errors, start the backend on port `8340` before running `npm run dev`.
- If screen or Notes access is incomplete, macOS permissions for Screen Recording / Automation may need to be granted.

## License

Free for personal, non-commercial use. Commercial use requires a license. See [LICENSE](LICENSE).
