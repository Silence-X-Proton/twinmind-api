#!/usr/bin/env bash
# TwinMind — fully automatic launcher.
#
# ONE COMMAND. No manual setup, no pip/npm/key/export steps:
#
#   ./run.sh
#
# It will:
#   * create the Python venv and install requirements (auto)
#   * install Node.js if missing, then Claude Code CLI (auto)
#   * start the OpenAI-compatible gateway AND the Claude Code Studio
#   * route Claude Code through the built-in TwinMind bridge when there is
#     no Anthropic key, so it runs on TwinMind models out of the box
#
# URLs printed at the end: /agent (studio), /admin (dashboard), /v1 (API).
set -uo pipefail
cd "$(dirname "$0")"

say(){ printf '\033[1;36m[*]\033[0m %s\n' "$*"; }
ok(){ printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

# ---- config (override via env) ----
export TWINMIND_POOL_SIZE="${TWINMIND_POOL_SIZE:-15}"
export TWINMIND_PORT="${TWINMIND_PORT:-8080}"
export TWINMIND_HOST="${TWINMIND_HOST:-0.0.0.0}"
export TWINMIND_MAX_RETRIES="${TWINMIND_MAX_RETRIES:-6}"
export TWINMIND_STATE_FILE="${TWINMIND_STATE_FILE:-$(pwd)/pool_state.json}"

# ---- 1) Python venv + deps ----
say "Setting up Python environment"
if [ ! -d .venv ]; then
  python3 -m venv .venv || { warn "python3 -m venv failed"; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip >/dev/null 2>&1 || true
pip install -q -r requirements.txt || { warn "pip install failed"; exit 1; }
ok "Python deps ready"

# ---- 2) Node.js (needed by Claude Code CLI) ----
if ! command -v node >/dev/null 2>&1; then
  say "Node.js not found — installing"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y -q >/dev/null 2>&1 || true
    apt-get install -y -q nodejs npm >/dev/null 2>&1 || true
  fi
fi
if command -v node >/dev/null 2>&1; then
  ok "Node.js $(node -v)"
else
  warn "Node.js missing — Claude Code engine will be unavailable (chat engine still works)"
fi

# ---- 3) Claude Code CLI ----
if ! command -v claude >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    say "Installing Claude Code CLI"
    npm install -g @anthropic-ai/claude-code@latest >/dev/null 2>&1 || \
      warn "Claude Code CLI install failed"
  else
    warn "npm missing — cannot install Claude Code CLI"
  fi
fi
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI $(claude --version 2>/dev/null | head -1)"
else
  warn "Claude Code CLI not installed"
fi

# ---- 4) announce engine mode ----
if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  ok "Anthropic key detected — Claude Code uses the official API"
else
  ok "No Anthropic key — Claude Code will run on TwinMind models via the built-in bridge"
fi

# ---- 5) run ----
PORT="$TWINMIND_PORT"
echo
echo "============================================================"
ok "Claude Code Studio:  http://127.0.0.1:$PORT/agent"
ok "Admin dashboard:    http://127.0.0.1:$PORT/admin"
ok "OpenAI API base:    http://127.0.0.1:$PORT/v1"
echo "============================================================"
echo
exec python3 app.py
