#!/usr/bin/env bash
# Stops whatever start.sh started: helena_server and, if this stack was the
# one that launched it, ollama. Processes not started by start.sh (no
# pidfile, or a pidfile pointing at a since-recycled pid) are left alone.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_DIR=".helena/run"

stop_pidfile() {
  local name="$1" pidfile="$2" match="$3"

  if [ ! -f "$pidfile" ]; then
    echo "$name: no pidfile, nothing to do"
    return
  fi

  local pid
  pid="$(cat "$pidfile")"

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name: not running (stale pidfile)"
    rm -f "$pidfile"
    return
  fi

  if ! ps -p "$pid" -o args= 2>/dev/null | grep -q "$match"; then
    echo "$name: pid $pid is no longer $name, leaving it alone"
    rm -f "$pidfile"
    return
  fi

  echo "$name: stopping (pid $pid)..."
  kill "$pid" 2>/dev/null
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$name: still up after 10s, sending SIGKILL"
    kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$pidfile"
  echo "$name: stopped"
}

stop_pidfile "helena_server" "$RUN_DIR/server.pid" "helena_server"
stop_pidfile "ollama" "$RUN_DIR/ollama.pid" "ollama"
