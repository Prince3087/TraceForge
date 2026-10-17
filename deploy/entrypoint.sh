#!/usr/bin/env bash
# Container entrypoint. One Cloud Run container, one public port ($PORT):
#   - FastAPI  -> 127.0.0.1:8000 (internal)      [TRACEFORGE_MODE=all|api]
#   - Streamlit -> 0.0.0.0:$PORT (public cockpit) [TRACEFORGE_MODE=all|ui]
# If either process exits, the container exits so Cloud Run restarts it.
set -euo pipefail

PORT="${PORT:-8080}"
MODE="${TRACEFORGE_MODE:-all}"
export TRACEFORGE_API_URL="${TRACEFORGE_API_URL:-http://127.0.0.1:8000}"

case "$MODE" in
  api)
    exec uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
    ;;
  ui)
    exec streamlit run frontend/app.py --server.address 0.0.0.0 --server.port "$PORT"
    ;;
  all)
    uvicorn backend.main:app --host 127.0.0.1 --port 8000 --proxy-headers &
    API_PID=$!
    # Wait for the API before the UI starts issuing requests.
    for _ in $(seq 1 50); do
      python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1)" 2>/dev/null && break
      kill -0 "$API_PID" 2>/dev/null || { echo "API failed to start" >&2; exit 1; }
      sleep 0.2
    done
    streamlit run frontend/app.py --server.address 0.0.0.0 --server.port "$PORT" &
    UI_PID=$!
    trap 'kill "$API_PID" "$UI_PID" 2>/dev/null || true' TERM INT
    wait -n "$API_PID" "$UI_PID"
    EXIT=$?
    kill "$API_PID" "$UI_PID" 2>/dev/null || true
    exit "$EXIT"
    ;;
  *)
    echo "Unknown TRACEFORGE_MODE='$MODE' (use all|api|ui)" >&2
    exit 2
    ;;
esac
