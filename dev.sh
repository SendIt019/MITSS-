#!/usr/bin/env bash
# Start the backend and the frontend together for local development.
# Stop both with Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f backend/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . backend/.env
  set +a
  echo "loaded backend/.env (provider: ${MITSS_LLM_PROVIDER:-manual})"
fi

if [ ! -d frontend/node_modules ]; then
  echo "installing frontend dependencies..."
  ( cd frontend && npm install )
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

( cd backend && python3 -m uvicorn app.main:app --reload --port 8000 ) &
( cd frontend && npm run dev ) &

echo
echo "  backend   http://127.0.0.1:8000/docs"
echo "  frontend  http://127.0.0.1:5173"
echo
wait
