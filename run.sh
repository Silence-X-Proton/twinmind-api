#!/usr/bin/env bash
# TwinMind OpenAI-compatible server launcher
set -euo pipefail
cd "$(dirname "$0")"

# ---- config (override via env) ----
export TWINMIND_POOL_SIZE="${TWINMIND_POOL_SIZE:-15}"
export TWINMIND_PORT="${TWINMIND_PORT:-8080}"
export TWINMIND_HOST="${TWINMIND_HOST:-0.0.0.0}"
export TWINMIND_MAX_RETRIES="${TWINMIND_MAX_RETRIES:-6}"
export TWINMIND_STATE_FILE="${TWINMIND_STATE_FILE:-$(pwd)/pool_state.json}"
# Optional: require an API key from clients (comma separated). Leave empty for open.
# export TWINMIND_API_KEYS="sk-your-key-1,sk-your-key-2"

# build venv if needed
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

exec python3 app.py
