#!/usr/bin/env bash
# One-command launcher for Google Colab (and any Linux box).
#
# In a Colab cell run:
#   !curl -fsSL https://raw.githubusercontent.com/Silence-X-Proton/twinmind-api/main/colab.sh | bash
#
# It will: install deps, start the API, open a Cloudflare tunnel, and print
# your public base URL (add /v1 for OpenAI clients, /admin for the dashboard).
set -uo pipefail

REPO="https://github.com/Silence-X-Proton/twinmind-api.git"
DIR="${TWINMIND_DIR:-/content/twinmind-api}"
PORT="${TWINMIND_PORT:-8080}"

say(){ echo -e "\033[1;36m[*]\033[0m $*"; }
ok(){ echo -e "\033[1;32m[+]\033[0m $*"; }

# 1) fetch code
if [ -d "$DIR/.git" ]; then
  say "Updating existing clone in $DIR"
  git -C "$DIR" pull -q || true
else
  say "Cloning repo to $DIR"
  git clone -q "$REPO" "$DIR"
fi
cd "$DIR"

# 2) deps
say "Installing Python dependencies"
pip install -q -r requirements.txt 2>/dev/null || pip install -q fastapi "uvicorn[standard]" httpx

# 3) cloudflared
if ! command -v cloudflared >/dev/null 2>&1; then
  say "Installing cloudflared"
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
  chmod +x /usr/local/bin/cloudflared
fi

# 4) start API
echo "--- killing old instances ---"
pkill -f 'python3 app.py' 2>/dev/null || true
pkill -f 'cloudflared tunnel' 2>/dev/null || true
sleep 2
say "Starting API server on :$PORT"
TWINMIND_POOL_SIZE="${TWINMIND_POOL_SIZE:-15}" nohup python3 app.py > server.log 2>&1 &
sleep 10

if command -v curl >/dev/null 2>&1; then
  echo "health: $(curl -s -m 10 http://127.0.0.1:$PORT/health || echo down)"
fi

# 5) tunnel
say "Starting Cloudflare tunnel"
nohup cloudflared tunnel --url "http://localhost:$PORT" > tunnel.log 2>&1 &
sleep 14
URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' tunnel.log | head -1)"

echo ""
echo "============================================================"
if [ -n "$URL" ]; then
  ok "PUBLIC BASE (OpenAI):   $URL/v1"
  ok "ADMIN DASHBOARD:        $URL/admin"
  ok "MODELS:                 $URL/v1/models"
else
  echo "[!] Tunnel URL not found yet. Check tunnel.log (it may need a few more seconds)."
fi
echo "============================================================"
echo "Local admin: http://127.0.0.1:$PORT/admin"
echo "Server log : tail -f $DIR/server.log"
echo "Tunnel log : tail -f $DIR/tunnel.log"
