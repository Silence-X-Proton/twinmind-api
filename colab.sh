#!/usr/bin/env bash
# One-command launcher for Google Colab (and any Linux box) with a continuous
# watchdog: it keeps the API + Cloudflare tunnel running until you DESTROY from
# the admin UI or the host/Colab shuts down.
#
# In a Colab cell run:
#   !curl -fsSL https://raw.githubusercontent.com/Silence-X-Proton/twinmind-api/main/colab.sh | bash
#
# Prints your public base URL (add /v1 for OpenAI clients, /admin for the UI).
#
# IMPORTANT:
#   * Colab blocks Cloudflare QUIC (UDP:7844) -> tunnel never connects.
#     We force --protocol http2 (TCP:443) so the tunnel always comes up.
#   * The watchdog tracks the server/tunnel by PID files (NOT pgrep): the
#     watchdog's own cmdline contains the words "python3 app.py"/"cloudflared",
#     so pgrep -f would match itself and never start anything.
set -uo pipefail

REPO="${TWINMIND_REPO:-https://github.com/Silence-X-Proton/twinmind-api.git}"
DIR="${TWINMIND_DIR:-/content/twinmind-api}"
PORT="${TWINMIND_PORT:-8080}"
PIDFILE="$DIR/.watchdog"
PROTO="${TWINMIND_TUNNEL_PROTOCOL:-http2}"

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
  mkdir -p /usr/local/bin 2>/dev/null || true
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
  chmod +x /usr/local/bin/cloudflared
fi
cloudflared --version 2>/dev/null | head -1 || true

# 4) stop old watchdog + services (PID-file first, then best-effort pkill)
if [ -f "$PIDFILE" ]; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
fi
# kill by pidfiles the watchdog wrote
for pf in "$DIR/.server.pid" "$DIR/.tunnel.pid"; do
  [ -f "$pf" ] && kill "$(cat "$pf")" 2>/dev/null || true
  rm -f "$pf"
 done
# best-effort cleanup of orphans (pkill here runs from THIS script, not the watchdog)
pkill -f 'python3 app.py' 2>/dev/null || true
pkill -f 'cloudflared tunnel' 2>/dev/null || true
sleep 2

# 5) start the watchdog (keeps api + tunnel alive continuously)
# IMPORTANT: Colab kills the whole process group when the cell finishes.
# `nohup` only ignores SIGHUP -> children still die. `setsid` puts everything
# into a NEW session that escapes the process-group kill, so the stack keeps
# running after the cell ends. Redirect stdin from /dev/null + disown too.
say "Starting watchdog (auto-restarts server + tunnel, protocol=$PROTO)"
setsid env TWINMIND_DIR="$DIR" TWINMIND_PORT="$PORT" TWINMIND_POOL_SIZE="${TWINMIND_POOL_SIZE:-15}" TWINMIND_TUNNEL_PROTOCOL="$PROTO" bash -c '
DIR="$TWINMIND_DIR"; PORT="$TWINMIND_PORT"; PROTO="$TWINMIND_TUNNEL_PROTOCOL"
cd "$DIR"
alive(){ [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
while true; do
  # ---- server ----
  SRV_PID="$(cat .server.pid 2>/dev/null || true)"
  if ! alive "$SRV_PID"; then
    echo "[watchdog] starting api on :$PORT"
    # setsid -> new session, survives Colab cell teardown
    setsid env TWINMIND_POOL_SIZE="$TWINMIND_POOL_SIZE" python3 app.py </dev/null > server.log 2>&1 &
    echo $! > .server.pid
  fi
  # ---- tunnel (http2 = TCP, required on Colab where UDP/QUIC is blocked) ----
  TUN_PID="$(cat .tunnel.pid 2>/dev/null || true)"
  if ! alive "$TUN_PID"; then
    echo "[watchdog] starting tunnel (protocol=$PROTO)"
    # setsid -> new session, survives Colab cell teardown
    setsid cloudflared tunnel --url "http://localhost:$PORT" --protocol "$PROTO" --no-autoupdate </dev/null > tunnel.log 2>&1 &
    echo $! > .tunnel.pid
  fi
  # persist the current public URL for later retrieval from any cell
  if [ -s tunnel.log ]; then
    sed -r "s/\x1B\[[0-9;]*[mK]//g" tunnel.log 2>/dev/null \
      | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1 > public_url.txt 2>/dev/null || true
  fi
  sleep 8
done
' </dev/null > watchdog.log 2>&1 &
WPID=$!
echo $WPID > "$PIDFILE"
disown 2>/dev/null || true

echo "--- waiting for services to come up ---"
# wait for local health (retry up to ~40s)
HEALTH="down"
for i in $(seq 1 20); do
  HEALTH="$(curl -s -m 5 http://127.0.0.1:$PORT/health || true)"
  [ -n "$HEALTH" ] && break
  sleep 2
done
echo "health (local): ${HEALTH:-down}"

# strip ANSI + grep URL (cloudflared writes progress to stderr AND stdout)
find_url() {
  sed -r "s/\x1B\[[0-9;]*[mK]//g" tunnel.log 2>/dev/null \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1
}
URL="$(find_url)"
for i in $(seq 1 15); do
  [ -n "$URL" ] && break
  sleep 4
  URL="$(find_url)"
done

echo ""
echo "============================================================"
if [ -n "$URL" ]; then
  ok "PUBLIC BASE (OpenAI):   $URL/v1"
  ok "ADMIN DASHBOARD:        $URL/admin"
  ok "MODELS:                 $URL/v1/models"
else
  echo "[!] Tunnel URL not found yet. Check logs:"
  echo "    tail -n 50 $DIR/tunnel.log"
  echo "    tail -n 50 $DIR/watchdog.log"
  echo "    grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' $DIR/tunnel.log | head -1"
fi
echo "============================================================"
echo "The watchdog keeps everything alive automatically."
echo "It stops only when you DESTROY from /admin (4x confirm), or the host/Colab ends."
echo "Logs:  tail -f $DIR/server.log   |   tail -f $DIR/watchdog.log   |   tail -f $DIR/tunnel.log"