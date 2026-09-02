#!/bin/bash
# FixO — 1-Click Startup Script

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=========================================="
echo " Starting FixO Voice AI Assistant...      "
echo "=========================================="

# 1. Ensure Ollama is running
if ! pgrep -x "ollama" > /dev/null; then
    echo "Starting Ollama..."
    ollama serve > /dev/null 2>&1 &
    sleep 2
fi

# 2. Start Backend Server
echo "Starting Backend Server on port 8340..."
if [ -f ".venv/bin/python" ]; then
    .venv/bin/python server.py --port 8340 --reload &
else
    python3 server.py --port 8340 --reload &
fi
BACKEND_PID=$!

# Wait for backend server to be ready before starting frontend
echo "Waiting for Backend Server to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:8340/api/health > /dev/null 2>&1; then
        break
    fi
    sleep 0.2
done

# 3. Start Frontend Dev Server
echo "Starting Frontend Dev Server on port 5173..."
cd frontend
npm run dev -- --host &
FRONTEND_PID=$!
cd "$DIR"

# Wait a moment for Vite dev server
sleep 1.5

# 4. Open Google Chrome to FixO
echo "Opening FixO in Google Chrome..."
open -a "Google Chrome" "http://localhost:5173"

echo "=========================================="
echo " FixO is active at http://localhost:5173  "
echo " Press Ctrl+C in this terminal to stop.  "
echo "=========================================="

# Handle clean exit
trap "kill $BACKEND_PID $FRONTEND_PID; exit" SIGINT SIGTERM
wait
