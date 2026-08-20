#!/usr/bin/env bash
# Starts the H.E.L.E.N.A stack: Ollama (if not already running) and
# helena_server, both as background processes with pidfiles + logs under
# .helena/run/. Once this returns, run `helena` to use the terminal agent.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_DIR=".helena/run"
mkdir -p "$RUN_DIR"

HOST="${HELENA_HOST:-127.0.0.1}"
PORT="${HELENA_PORT:-8080}"
OLLAMA_HOST="${HELENA_OLLAMA_HOST:-http://127.0.0.1:11434}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PYTHON="$(command -v python3 || command -v python)"
if [ -z "$PYTHON" ]; then
  echo "error: no python3/python on PATH" >&2
  exit 1
fi

wait_for() {
  local url="$1" tries="${2:-30}"
  for _ in $(seq 1 "$tries"); do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# --- Ollama ------------------------------------------------------------
if curl -fsS "$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  echo "ollama:       already running at $OLLAMA_HOST"
else
  if ! command -v ollama >/dev/null 2>&1; then
    echo "error: ollama not found and nothing answering at $OLLAMA_HOST" >&2
    echo "       install it from https://ollama.com" >&2
    exit 1
  fi
  echo "ollama:       starting..."
  nohup ollama serve >"$RUN_DIR/ollama.log" 2>&1 &
  echo $! >"$RUN_DIR/ollama.pid"
  if wait_for "$OLLAMA_HOST/api/version"; then
    echo "ollama:       started (pid $(cat "$RUN_DIR/ollama.pid"))"
  else
    echo "error: ollama did not become ready, check $RUN_DIR/ollama.log" >&2
    exit 1
  fi
fi

# --- helena_server -------------------------------------------------------
if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
  echo "helena_server: already running at http://$HOST:$PORT"
else
  echo "helena_server: starting..."
  nohup "$PYTHON" -m helena_server --host "$HOST" --port "$PORT" --log-level warning \
    >"$RUN_DIR/server.log" 2>&1 &
  echo $! >"$RUN_DIR/server.pid"
  if wait_for "http://$HOST:$PORT/health"; then
    echo "helena_server: started (pid $(cat "$RUN_DIR/server.pid")) at http://$HOST:$PORT"
  else
    echo "error: helena_server did not become ready, check $RUN_DIR/server.log" >&2
    exit 1
  fi
fi

echo
echo "H.E.L.E.N.A is up. Run 'helena' to start the terminal agent, or ./stop.sh to shut it down."
