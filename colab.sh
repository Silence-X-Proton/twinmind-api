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
#   * IDEMPOTENT: running this again does NOT restart anything if the stack is
#     already healthy. A quick tunnel gets a NEW random URL every restart, so
#     restarting would silently kill the /admin link you saved (Cloudflare edge
#     then shows a bare 404 "No web page was found"). Re-run = same URL.
#     Force a restart with:  TWINMIND_RESTART=1
#   * STABLE URL (optional): set TWINMIND_TUNNEL_TOKEN=<token> to use a named
#     Cloudflare tunnel -> the URL never changes across restarts.
set -uo pipefail

REPO="${TWINMIND_REPO:-https://github.com/Silence-X-Proton/twinmind-api.git}"
DIR="${TWINMIND_DIR:-/content/twinmind-api}"
PORT="${TWINMIND_PORT:-8080}"
PIDFILE="$DIR/.watchdog"
PROTO="${TWINMIND_TUNNEL_PROTOCOL:-http2}"
TOKEN="${TWINMIND_TUNNEL_TOKEN:-}"
RESTART="${TWINMIND_RESTART:-0}"

say(){ echo -e "\033[1;36m[*]\033[0m $*"; }
ok(){ echo -e "\033[1;32m[+]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[!]\033[0m $*"; }

# Extract the current public URL (from public_url.txt, then tunnel.log)
current_url(){
  local u=""
  [ -s "$DIR/public_url.txt" ] && u="$(head -1 "$DIR/public_url.txt" 2>/dev/null)"
  if [ -z "$u" ] && [ -s "$DIR/tunnel.log" ]; then
    u="$(sed -r 's/\x1B\[[0-9;]*[mK]//g' "$DIR/tunnel.log" 2>/dev/null \
        | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)"
  fi
  printf '%s' "$u"
}

print_urls(){
  local u="$1"
  echo ""
  echo "============================================================"
  if [ -n "$u" ]; then
    ok "CLAUDE CODE STUDIO:     $u/agent"
    ok "PUBLIC BASE (OpenAI):   $u/v1"
    ok "ADMIN DASHBOARD:        $u/admin"
    ok "MODELS:                 $u/v1/models"
  else
    echo "[!] Tunnel URL not found yet. Check logs or run: bash $DIR/url.sh"
    echo "    tail -n 30 $DIR/tunnel.log"
    echo "    tail -n 30 $DIR/watchdog.log"
  fi
  echo "============================================================"
}

# Is an instance already up + FULLY healthy AND not asked to restart?
# Healthy means: watchdog alive AND server answering AND the tunnel process alive.
# We must check the tunnel too: a quick tunnel that died (and got auto-restarted)
# gets a NEW URL, so the old /admin link turns into Cloudflare 1033/404. If the
# tunnel is dead we MUST NOT "reuse" -> the restart path repairs it.
reuse_if_healthy(){
  [ "$RESTART" = "1" ] && return 1
  [ -f "$PIDFILE" ] || return 1
  local wp; wp="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$wp" ] && kill -0 "$wp" 2>/dev/null || return 1
  # server liveness
  local h; h="$(curl -s -m 4 "http://127.0.0.1:$PORT/health" || true)"
  [ -n "$h" ] || return 1
  # tunnel liveness (PID-file tracked by the watchdog)
  local tp; tp="$(cat "$DIR/.tunnel.pid" 2>/dev/null || true)"
  { [ -n "$tp" ] && kill -0 "$tp" 2>/dev/null; } || return 1
  # and we must actually know the current public URL
  [ -n "$(current_url)" ] || return 1
  return 0
}

# --------------------------------------------------------------------------- #
# FAST PATH: already running -> reuse, never rotate the URL accidentally.
# --------------------------------------------------------------------------- #
if [ -d "$DIR/.git" ] && reuse_if_healthy; then
  URL="$(current_url)"
  say "TwinMind is ALREADY running — reusing it (no restart, URL unchanged)."
  echo "    (force a fresh restart with:  TWINMIND_RESTART=1 bash colab.sh)"
  print_urls "$URL"
  echo "health (local): $(curl -s -m 5 http://127.0.0.1:$PORT/health 2>/dev/null || echo down)"
  exit 0
fi

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
pip install -q -r requirements.txt 2>/dev/null || pip install -q fastapi "uvicorn[standard]" httpx python-multipart

# 2b) Node.js + Claude Code CLI (for the /agent studio). Colab ships node already;
# on a bare box we install it. Never fatal: the chat engine still works without it.
if ! command -v node >/dev/null 2>&1; then
  say "Installing Node.js"
  { apt-get update -y -q && apt-get install -y -q nodejs npm; } >/dev/null 2>&1 || true
fi
if command -v npm >/dev/null 2>&1; then
  if ! command -v claude >/dev/null 2>&1; then
    say "Installing Claude Code CLI"
    npm install -g @anthropic-ai/claude-code@latest >/dev/null 2>&1 || true
  fi
  command -v claude >/dev/null 2>&1 && ok "Claude Code CLI $(claude --version 2>/dev/null | head -1)"
fi

# 3) cloudflared
if ! command -v cloudflared >/dev/null 2>&1; then
  say "Installing cloudflared"
  mkdir -p /usr/local/bin 2>/dev/null || true
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
  chmod +x /usr/local/bin/cloudflared
fi
cloudflared --version 2>/dev/null | head -1 || true

# 4) stop old watchdog + services (PID-file first, then best-effort pkill)
warn "Restarting stack (this rotates a quick-tunnel URL)"
if [ -f "$PIDFILE" ]; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
fi
for pf in "$DIR/.server.pid" "$DIR/.tunnel.pid"; do
  [ -f "$pf" ] && kill "$(cat "$pf")" 2>/dev/null || true
  rm -f "$pf"
 done
pkill -f 'python3 app.py' 2>/dev/null || true
pkill -f 'cloudflared tunnel' 2>/dev/null || true
sleep 2

# 5) start the watchdog (keeps api + tunnel alive continuously)
# IMPORTANT: Colab kills the whole process group when the cell finishes.
# `nohup` only ignores SIGHUP -> children still die. `setsid` puts everything
# into a NEW session that escapes the process-group kill, so the stack keeps
# running after the cell ends. Redirect stdin from /dev/null + disown too.
say "Starting watchdog (auto-restarts server + tunnel, protocol=$PROTO)"
setsid env TWINMIND_DIR="$DIR" TWINMIND_PORT="$PORT" TWINMIND_POOL_SIZE="${TWINMIND_POOL_SIZE:-15}" TWINMIND_TUNNEL_PROTOCOL="$PROTO" TWINMIND_TUNNEL_TOKEN="$TOKEN" bash -c '
DIR="$TWINMIND_DIR"; PORT="$TWINMIND_PORT"; PROTO="$TWINMIND_TUNNEL_PROTOCOL"; TOKEN="$TWINMIND_TUNNEL_TOKEN"
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
  # ---- tunnel ----
  TUN_PID="$(cat .tunnel.pid 2>/dev/null || true)"
  if ! alive "$TUN_PID"; then
    if [ -n "$TOKEN" ]; then
      echo "[watchdog] starting NAMED tunnel (stable URL)"
      setsid cloudflared tunnel --no-autoupdate run --token "$TOKEN" </dev/null > tunnel.log 2>&1 &
    else
      echo "[watchdog] starting quick tunnel (protocol=$PROTO)"
      # http2 = TCP, required on Colab where UDP/QUIC is blocked
      setsid cloudflared tunnel --url "http://localhost:$PORT" --protocol "$PROTO" --no-autoupdate </dev/null > tunnel.log 2>&1 &
    fi
    echo $! > .tunnel.pid
  fi
  # persist the current public URL (ONLY when we actually found one -> never blank it)
  if [ -s tunnel.log ]; then
    NEWURL="$(sed -r "s/\x1B\[[0-9;]*[mK]//g" tunnel.log 2>/dev/null | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1)"
    [ -n "$NEWURL" ] && printf "%s\n" "$NEWURL" > public_url.txt
  fi
  sleep 8
done
' </dev/null > watchdog.log 2>&1 &
WPID=$!
echo $WPID > "$PIDFILE"
disown 2>/dev/null || true

# 6) write a tiny url.sh helper so you can always fetch the current URL
echo 'true' >/dev/null
cat > "$DIR/url.sh" <<'EOF'
#!/usr/bin/env bash
DIR="${TWINMIND_DIR:-/content/twinmind-api}"
u=""
[ -s "$DIR/public_url.txt" ] && u="$(head -1 "$DIR/public_url.txt" 2>/dev/null)"
if [ -z "$u" ] && [ -s "$DIR/tunnel.log" ]; then
  u="$(sed -r 's/\x1B\[[0-9;]*[mK]//g' "$DIR/tunnel.log" 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)"
fi
echo "$u"
EOF
chmod +x "$DIR/url.sh"

echo "--- waiting for services to come up ---"
HEALTH="down"
for i in $(seq 1 20); do
  HEALTH="$(curl -s -m 5 http://127.0.0.1:$PORT/health || true)"
  [ -n "$HEALTH" ] && break
  sleep 2
done
echo "health (local): ${HEALTH:-down}"

find_url(){
  sed -r "s/\x1B\[[0-9;]*[mK]//g" tunnel.log 2>/dev/null \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1
}
URL="$(find_url)"
for i in $(seq 1 15); do
  [ -n "$URL" ] && break
  sleep 4
  URL="$(find_url)"
done
[ -n "$URL" ] && printf '%s\n' "$URL" > public_url.txt

# Verify the URL is actually routable through Cloudflare's edge before we call
# it a success. A freshly registered quick tunnel can take a few seconds to
# become reachable, and a dead link shows Cloudflare 1033/404.
if [ -n "$URL" ]; then
  say "Verifying public URL through Cloudflare edge…"
  EDGE=""
  for i in $(seq 1 12); do
    EDGE="$(curl -s4 -m 10 -o /dev/null -w '%{http_code}' "$URL/health" 2>/dev/null || true)"
    [ "$EDGE" = "200" ] && break
    sleep 5
  done
  if [ "$EDGE" = "200" ]; then
    ok "public edge check: HTTP 200 (URL is live)"
  else
    warn "public edge check: HTTP ${EDGE:-?} — URL not reachable yet (tunnel may still be starting, or a stale link)"
  fi
fi

print_urls "$URL"
echo "The watchdog keeps everything alive automatically."
echo "Re-running this command will REUSE the running instance (URL unchanged)."
echo "Fetch the current URL anytime:  bash $DIR/url.sh"
echo "Logs:  tail -f $DIR/server.log   |   tail -f $DIR/watchdog.log   |   tail -f $DIR/tunnel.log"
