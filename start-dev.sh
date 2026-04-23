#!/usr/bin/env bash
# Start the engram-harness dev environment (WSL / Git Bash / macOS)
#  - FastAPI backend on http://localhost:8420
#  - Vite frontend on http://localhost:5173

set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
  echo "Stopping servers..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting FastAPI backend on :8420 ..."
python -m uvicorn harness.server:app --host 127.0.0.1 --port 8420 --reload &
BACKEND_PID=$!

echo "Starting Vite dev server on :5173 ..."
(cd "$ROOT/frontend" && npm run dev) &
FRONTEND_PID=$!

echo ""
echo "  Backend:  http://localhost:8420"
echo "  Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both servers."
wait
