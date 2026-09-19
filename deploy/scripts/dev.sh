#!/usr/bin/env bash
# EMUNEL — local development launcher.
# Starts PostgreSQL check, Console API, Worker, and (optionally) a Core instance.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Defaults (override via env)
export EMUNEL_DATABASE_URL="${EMUNEL_DATABASE_URL:-postgresql://admin:emunel@127.0.0.1:5432/emunel}"
export EMUNEL_SECRET_KEY="${EMUNEL_SECRET_KEY:-dev-secret-key-0123456789abcdef0123456789abcdef}"
export EMUNEL_WORKER_TOKEN="${EMUNEL_WORKER_TOKEN:-dev-worker-token-0123456789abcdef}"
export EMUNEL_GITHUB_CLIENT_ID="${EMUNEL_GITHUB_CLIENT_ID:-}"
export EMUNEL_GITHUB_CLIENT_SECRET="${EMUNEL_GITHUB_CLIENT_SECRET:-}"
export EMUNEL_PUBLIC_URL="${EMUNEL_PUBLIC_URL:-http://127.0.0.1:8080}"
export EMUNEL_LOCAL_WORKER_URL="${EMUNEL_LOCAL_WORKER_URL:-http://127.0.0.1:9100}"
export EMUNEL_WORKER_DATA="${EMUNEL_WORKER_DATA:-/tmp/emunel-dev/instances}"
export PYTHONUNBUFFERED=1

say() { printf '\033[1;36memunel-dev\033[0m %s\n' "$1"; }

cleanup() {
  say "shutting down…"
  [ -n "${CONSOLE_PID:-}" ] && kill "$CONSOLE_PID" 2>/dev/null || true
  [ -n "${WORKER_PID:-}" ] && kill "$WORKER_PID" 2>/dev/null || true
}
trap cleanup EXIT

say "starting Console API on :8080"
(cd "$ROOT/console/api" && . .venv/bin/activate && python -m emunel_console) &
CONSOLE_PID=$!

say "starting Worker on :9100 (process driver, dev isolation)"
(cd "$ROOT/worker" && . .venv/bin/activate && \
  PYTHONPATH="$ROOT/core" EMUNEL_WORKER_DRIVER="${EMUNEL_WORKER_DRIVER:-process}" \
  python -m emunel_worker) &
WORKER_PID=$!

say "console  → http://127.0.0.1:8080"
say "worker   → http://127.0.0.1:9100"
say "press Ctrl+C to stop"
wait
