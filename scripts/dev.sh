#!/usr/bin/env bash
# Run the Sift backend and the Vite dev server together. Stops only the processes it
# started (never its own process group), and fails if either one stops on its own.
set -u

cd "$(dirname "$0")/.."

backend_pid=""
vite_pid=""
cleaned=0

# Kill a process and everything it spawned (uv/npm wrap the real servers).
kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill "$pid" 2>/dev/null
}

cleanup() {
  [ "$cleaned" -eq 1 ] && return
  cleaned=1
  trap - INT TERM EXIT
  local pid
  for pid in "$backend_pid" "$vite_pid"; do
    [ -n "$pid" ] && kill_tree "$pid"
  done
  for pid in "$backend_pid" "$vite_pid"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null
  done
}

trap 'cleanup; exit 0' INT
trap 'cleanup; exit 143' TERM
trap 'cleanup' EXIT

SIFT_HOME=.dev SIFT_DEV_TOKEN=dev uv run sift --no-window --port 8765 &
backend_pid=$!
npm --prefix web run dev &
vite_pid=$!

while :; do
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    echo "Sift backend stopped; shutting down the dev server." >&2
    exit 1
  fi
  if ! kill -0 "$vite_pid" 2>/dev/null; then
    echo "Vite dev server stopped; shutting down the Sift backend." >&2
    exit 1
  fi
  sleep 0.5
done
