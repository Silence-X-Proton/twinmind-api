#!/usr/bin/env bash
# One-command launcher for Google Colab (and any Linux box) with a continuous
# watchdog: it keeps the API + Cloudflare tunnel running until you DESTROY from
# the admin UI or the host/Colab shuts down.
#
# In a Colab cell run:
#   !curl -fsSL https://raw.githubusercontent.com/Silence-X-Proton/twinmind-api/main/colab.sh | bash
#
# Prints your public base URL (add /v1 for OpenAI clients, /admin for the UI).
set -uo pipefail

REPO="https://github.com/Silence-X-Proton/twinmind-api.git"
DIR="${TWINMIND_DIR:-/content/twinmind-api}"
PORT="${TWINMIND_PORT:-8080}"
PIDFILE="$DIR/.watchdog"

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

# 4) stop old instances + watchdog
if [ -f "$PIDFILE" ]; then kill "$(cat "$PIDFILE")" 2>/dev/null || true; rm -f "$PIDFILE"; fi
pkill -f 'python3 app.py' 2>/dev/null || true
pkill -f 'cloudflared' 2>/dev/null || true
sleep 2

# 5) start the watchdog (keeps api + tunnel alive continuously)
say "Starting watchdog (auto-restarts server + tunnel)"
nohup env TWINMIND_DIR="$DIR" TWINMIND_PORT="$PORT" TWINMIND_POOL_SIZE="${TWINMIND_POOL_SIZE:-15}" bash -c '
DIR="$TWINMIND_DIR"; PORT="$TWINMIND_PORT"
cd "$DIR"
while true; do
  # server
  if ! pgrep -f "python3 app.py" >/dev/null 2>&1; then
    echo "[watchdog] starting api on :$PORT"
    TWINMIND_POOL_SIZE="$TWINMIND_POOL_SIZE" nohup python3 app.py > server.log 2>&1 &
  fi
  # tunnel
  if ! pgrep -f "cloudflared" >/dev/null 2>&1; then
    echo "[watchdog] starting tunnel"
    nohup cloudflared tunnel --url "http://localhost:$PORT" > tunnel.log 2>&1 &
  fi
  sleep 8
done
' > watchdog.log 2>&1 &
echo $! > "$PIDFILE"

echo "--- waiting for services to come up ---"
sleep 16

echo "health (local): $(curl -s -m 10 http://127.0.0.1:$PORT/health || echo down)"
URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' tunnel.log 2>/dev/null | head -1)"

for i in 1 2 3 4 5 6; do
  [ -n "$URL" ] && break
  sleep 4
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' tunnel.log 2>/dev/null | head -1)"
done

echo ""
echo "============================================================"
if [ -n "$URL" ]; then
  ok "PUBLIC BASE (OpenAI):   $URL/v1"
  ok "ADMIN DASHBOARD:        $URL/admin"
  ok "MODELS:                 $URL/v1/models"
else
  echo "[!] Tunnel URL not found yet. Run:  grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' $DIR/tunnel.log | head -1"
fi
echo "============================================================"
echo "The watchdog keeps everything alive automatically."
echo "It stops only when you DESTROY from /admin (4x confirm), or the host/Colab ends."
echo "Logs:  tail -f $DIR/server.log   |   tail -f $DIR/watchdog.log"
