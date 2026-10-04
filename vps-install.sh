#!/usr/bin/env bash
# TwinMind Gateway — one-command installer for an Ubuntu/Debian VPS.
#
# Install (as root):
#   curl -fsSL https://raw.githubusercontent.com/Silence-X-Proton/twinmind-api/main/vps-install.sh | bash
#
# With a STABLE Cloudflare named-tunnel URL (recommended, URL never changes):
#   curl -fsSL .../vps-install.sh | TWINMIND_TUNNEL_TOKEN='<token>' bash
#
# Options (env):
#   TWINMIND_DIR=/opt/twinmind-api      install dir
#   TWINMIND_PORT=8080                  local API port
#   TWINMIND_POOL_SIZE=15               account pool size
#   TWINMIND_ADMIN_KEY=secret           lock the /admin UI
#   TWINMIND_API_KEYS=sk-1,sk-2         require API keys for /v1/*
#   TWINMIND_TUNNEL_TOKEN=<token>       use a named (stable) Cloudflare tunnel
#   NO_TUNNEL=1                          skip Cloudflare (use server's own IP/domain)
#
# Uninstall:
#   curl -fsSL .../vps-install.sh | bash -s -- --uninstall
set -uo pipefail

REPO="${TWINMIND_REPO:-https://github.com/Silence-X-Proton/twinmind-api.git}"
DIR="${TWINMIND_DIR:-/opt/twinmind-api}"
PORT="${TWINMIND_PORT:-8080}"
POOL="${TWINMIND_POOL_SIZE:-15}"
TOKEN="${TWINMIND_TUNNEL_TOKEN:-}"
NO_TUNNEL="${NO_TUNNEL:-0}"
PROTO="${TWINMIND_TUNNEL_PROTOCOL:-http2}"
ENVFILE="/etc/default/twinmind"
UNIT_API="/etc/systemd/system/twinmind.service"
UNIT_TUN="/etc/systemd/system/twinmind-tunnel.service"

c_ok(){ echo -e "\033[1;32m[+]\033[0m $*"; }
c_in(){ echo -e "\033[1;36m[*]\033[0m $*"; }
c_wn(){ echo -e "\033[1;33m[!]\033[0m $*"; }
c_er(){ echo -e "\033[1;31m[x]\033[0m $*"; }

have(){ command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- animation --
# Live terminal UX: a banner, a step counter and a spinner while long commands
# run, so the single installer command visibly "installs everything" step by
# step instead of silently hanging.
STEP=0
TOTAL=8
BANNER(){
  echo -e "\033[1;35m"
  cat <<'BANNER_EOF'
   ___________       __  _______ .__            .___
   \__    ___/__  _|__|/  |    |/ _| ____   __| _/
     |    |  \  \/ /  |   |  |  \  |/    \ / __ |
     |    |   \   /|  |   |  |  /  |   |  / /_/ |
     |____|   \_/ |__|__|____/|__|___|  \____ |
                                       \/    \/
BANNER_EOF
  echo -e "\033[0m"
  echo -e "\033[1;36m   TwinMind Gateway + Claude Code Studio installer\033[0m"
  echo -e "\033[2m   one command -> OpenAI API + AI agent studio + public URL\033[0m"
  echo ""
}

step_title(){ STEP=$((STEP+1)); echo -e "\033[1;36m\n[${STEP}/${TOTAL}] $*\033[0m"; }

# spin "message" command...  -> shows an animated spinner until the command ends
spin(){
  local msg="$1"; shift
  if [ ! -t 1 ]; then "$@"; return $?; fi
  local frames='|/-\\' i=0 pid rc
  "$@" >/tmp/twinmind_step.log 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    i=$(( (i+1) % 4 ))
    printf "\r   \033[1;33m%s\033[0m %-42s" "${frames:$i:1}" "$msg"
    sleep 0.15
  done
  wait "$pid"; rc=$?
  if [ $rc -eq 0 ]; then
    printf "\r   \033[1;32m✔\033[0m %-42s\033[0m\n" "$msg"
  else
    printf "\r   \033[1;31m✘\033[0m %-42s\033[0m\n" "$msg"
    [ -s /tmp/twinmind_step.log ] && tail -n 3 /tmp/twinmind_step.log | sed 's/^/       /'
  fi
  return $rc
}

# ------------------------------------------------------------------ uninstall --
if [ "${1:-}" = "--uninstall" ]; then
  c_in "Uninstalling TwinMind…"
  if [ -d /run/systemd/system ]; then
    systemctl disable --now twinmind.service twinmind-tunnel.service 2>/dev/null || true
    rm -f "$UNIT_API" "$UNIT_TUN"
    systemctl daemon-reload 2>/dev/null || true
  fi
  # fallback watchdog
  [ -f "$DIR/.watchdog" ] && kill "$(cat "$DIR/.watchdog")" 2>/dev/null || true
  [ -f "$DIR/.server.pid" ] && kill "$(cat "$DIR/.server.pid")" 2>/dev/null || true
  [ -f "$DIR/.tunnel.pid" ] && kill "$(cat "$DIR/.tunnel.pid")" 2>/dev/null || true
  # Only kill TwinMind's own process (match the full venv python path) — a bare
  # 'app.py' pattern would also match unrelated processes like '''webapp.py'''.
  pkill -f "$DIR/.venv/bin/python" 2>/dev/null || true
  pkill -f "cloudflared tunnel" 2>/dev/null || true
  # wait until really gone (kills can lag a few seconds)
  for i in $(seq 1 10); do
    pgrep -f "$DIR/.venv/bin/python" >/dev/null 2>&1 || { pgrep -f 'cloudflared tunnel' >/dev/null 2>&1 || break; }
    sleep 1
  done
  pkill -9 -f "$DIR/.venv/bin/python" 2>/dev/null || true
  pkill -9 -f "cloudflared tunnel" 2>/dev/null || true
  rm -f "$DIR/.watchdog" "$DIR/.server.pid" "$DIR/.tunnel.pid"
  # cron
  crontab -l 2>/dev/null | grep -v 'twinmind-api/vps-install.sh' | crontab - 2>/dev/null || true
  rm -f "$ENVFILE"
  c_ok "Removed services + env. Repo kept at $DIR (delete manually if you want)."
  exit 0
fi

# ------------------------------------------------------------------ preflight --
if [ "$(id -u)" != "0" ]; then
  c_er "Run as root:  sudo -i   then re-run."
  exit 1
fi

if ! have apt-get; then
  c_er "This installer targets Ubuntu/Debian (apt-get not found)."
  exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) CF=cloudflared-linux-amd64 ;;
  aarch64|arm64) CF=cloudflared-linux-arm64 ;;
  *) CF=cloudflared-linux-amd64 ;;
esac

BANNER
c_in "TwinMind VPS installer  (arch=$ARCH, dir=$DIR, port=$PORT)"

# ------------------------------------------------------------------ packages --
step_title "Installing system packages (Python, git, curl, Node.js, npm)"
export DEBIAN_FRONTEND=noninteractive
if ! have python3 || ! have git || ! have curl || ! have node; then
  spin "apt-get install base packages" bash -c 'apt-get update -y -q && apt-get install -y -q python3 python3-venv python3-pip git curl ca-certificates nodejs npm' || c_wn "some packages failed — continuing"
fi
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo '?')"
c_ok "python3 $PYV, git, curl present$(have node && printf ', node %s' "$(node -v)" || true)"

# ------------------------------------------------------------------- get code --
step_title "Fetching the TwinMind code"
if [ -d "$DIR/.git" ]; then
  spin "git pull in $DIR" git -C "$DIR" pull -q --ff-only || c_wn "git pull skipped (local changes?) — continuing"
else
  mkdir -p "$(dirname "$DIR")"
  spin "git clone into $DIR" git clone -q "$REPO" "$DIR"
fi
cd "$DIR" || { c_er "cannot cd $DIR"; exit 1; }

# ----------------------------------------------------------------------- venv --
step_title "Creating the Python venv and installing dependencies"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv"
fi
spin "pip install -r requirements.txt" bash -c "\"$DIR/.venv/bin/pip\" install -q --upgrade pip && { \"$DIR/.venv/bin/pip\" install -q -r \"$DIR/requirements.txt\" || \"$DIR/.venv/bin/pip\" install -q fastapi 'uvicorn[standard]' httpx python-multipart; }"
c_ok "dependencies installed"

# --------------------------------------------------------------- claude code --
# Node.js + Claude Code CLI so the /agent studio works on this host.
# Never fatal: without it the chat + gateway engines still run.
step_title "Installing the Claude Code CLI (for the /agent AI studio)"
export DEBIAN_FRONTEND=noninteractive
if ! have node; then
  spin "installing Node.js" bash -c 'apt-get update -y -q && apt-get install -y -q nodejs npm' || c_wn "node install failed"
fi
if have npm && ! have claude; then
  spin "npm install -g @anthropic-ai/claude-code" npm install -g @anthropic-ai/claude-code@latest || c_wn "Claude Code install failed (studio still needs it)"
fi
if have claude; then
  c_ok "Claude Code CLI $(claude --version 2>/dev/null | head -1)"
else
  c_wn "Claude Code CLI not available — /agent studio needs it"
fi

# ----------------------------------------------------------------- cloudflared --
step_title "Installing cloudflared (the public tunnel)"
if [ "$NO_TUNNEL" != "1" ]; then
  if ! have cloudflared; then
    spin "downloading cloudflared" bash -c "curl -fsSL 'https://github.com/cloudflare/cloudflared/releases/latest/download/${CF}' -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared" || c_wn "cloudflared install failed"
  fi
  have cloudflared && c_ok "cloudflared $(cloudflared --version 2>/dev/null | head -1 | awk '{print \$3}')"
else
  c_wn "NO_TUNNEL=1 — skipping cloudflared"
fi

# ------------------------------------------------------------------ env file --
step_title "Writing configuration"
{
  echo "TWINMIND_DIR=$DIR"
  echo "TWINMIND_HOST=0.0.0.0"
  echo "TWINMIND_PORT=$PORT"
  echo "TWINMIND_POOL_SIZE=$POOL"
  echo "TWINMIND_TUNNEL_PROTOCOL=$PROTO"
  [ -n "${TWINMIND_ADMIN_KEY:-}" ] && echo "TWINMIND_ADMIN_KEY=$TWINMIND_ADMIN_KEY"
  [ -n "${TWINMIND_API_KEYS:-}" ] && echo "TWINMIND_API_KEYS=$TWINMIND_API_KEYS"
} > "$ENVFILE"
chmod 600 "$ENVFILE"
c_ok "wrote $ENVFILE"

# --------------------------------------------------------------- systemd path --
USE_SYSTEMD=0
if [ -d /run/systemd/system ] && [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ]; then USE_SYSTEMD=1; fi

step_title "Configuring auto-start + launching the services"
if [ "$USE_SYSTEMD" = "1" ]; then
  c_in "systemd detected — installing services"
  cat > "$UNIT_API" <<EOF
[Unit]
Description=TwinMind Gateway API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$ENVFILE
ExecStart=$DIR/.venv/bin/python $DIR/app.py
Restart=always
RestartSec=3
StandardOutput=append:$DIR/server.log
StandardError=append:$DIR/server.log

[Install]
WantedBy=multi-user.target
EOF

  if [ "$NO_TUNNEL" = "1" ]; then
    rm -f "$UNIT_TUN"
  elif [ -n "$TOKEN" ]; then
    cat > "$UNIT_TUN" <<EOF
[Unit]
Description=TwinMind Cloudflare named tunnel (stable URL)
After=network-online.target twinmind.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
# token is a secret: keep it in the unit, restricted permissions
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate run --token $TOKEN
Restart=always
RestartSec=5
StandardOutput=append:$DIR/tunnel.log
StandardError=append:$DIR/tunnel.log

[Install]
WantedBy=multi-user.target
EOF
    chmod 600 "$UNIT_TUN"
  else
    cat > "$UNIT_TUN" <<EOF
[Unit]
Description=TwinMind Cloudflare quick tunnel (random URL)
After=network-online.target twinmind.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=/usr/local/bin/cloudflared tunnel --url http://localhost:$PORT --protocol $PROTO --no-autoupdate
Restart=always
RestartSec=5
StandardOutput=append:$DIR/tunnel.log
StandardError=append:$DIR/tunnel.log

[Install]
WantedBy=multi-user.target
EOF
  fi

  systemctl daemon-reload
  systemctl enable --now twinmind.service >/dev/null 2>&1
  c_ok "enabled + started twinmind.service"
  if [ -f "$UNIT_TUN" ]; then
    systemctl enable --now twinmind-tunnel.service >/dev/null 2>&1
    c_ok "enabled + started twinmind-tunnel.service"
  fi
  sleep 6
  echo ""
  echo "service: $(systemctl is-active twinmind.service 2>/dev/null)"
  echo "health : $(curl -s -m 5 http://127.0.0.1:$PORT/health 2>/dev/null || echo down)"
  echo "logs   : journalctl -u twinmind.service -f   (or tail -f $DIR/server.log)"
else
  # --------------------------------------------------------------- fallback path --
  c_wn "systemd is not PID 1 here — using a self-healing watchdog + @reboot cron"
  # stop any previous instance
  for pf in .watchdog .server.pid .tunnel.pid; do
    [ -f "$DIR/$pf" ] && kill "$(cat "$DIR/$pf")" 2>/dev/null || true
    rm -f "$DIR/$pf"
  done
  pkill -f "$DIR/.venv/bin/python" 2>/dev/null || true
  pkill -f 'cloudflared tunnel' 2>/dev/null || true
  sleep 2

  setsid env TWINMIND_DIR="$DIR" TWINMIND_PORT="$PORT" TWINMIND_POOL_SIZE="$POOL" \
    TWINMIND_TUNNEL_PROTOCOL="$PROTO" TWINMIND_TUNNEL_TOKEN="$TOKEN" NO_TUNNEL="$NO_TUNNEL" bash -c '
DIR="$TWINMIND_DIR"; PORT="$TWINMIND_PORT"; PROTO="$TWINMIND_TUNNEL_PROTOCOL"; TOKEN="$TWINMIND_TUNNEL_TOKEN"
cd "$DIR"
alive(){ [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
while true; do
  SRV="$(cat .server.pid 2>/dev/null || true)"
  if ! alive "$SRV"; then
    echo "[watchdog] start api :$PORT"
    setsid "$DIR/.venv/bin/python" app.py </dev/null > server.log 2>&1 & echo $! > .server.pid
  fi
  if [ "${NO_TUNNEL:-0}" != "1" ]; then
    TUN="$(cat .tunnel.pid 2>/dev/null || true)"
    if ! alive "$TUN"; then
      if [ -n "$TOKEN" ]; then
        echo "[watchdog] start named tunnel"
        setsid cloudflared tunnel --no-autoupdate run --token "$TOKEN" </dev/null > tunnel.log 2>&1 & echo $! > .tunnel.pid
      else
        echo "[watchdog] start quick tunnel ($PROTO)"
        setsid cloudflared tunnel --url "http://localhost:$PORT" --protocol "$PROTO" --no-autoupdate </dev/null > tunnel.log 2>&1 & echo $! > .tunnel.pid
      fi
    fi
    if [ -s tunnel.log ]; then
      NU="$(sed -r "s/\x1B\[[0-9;]*[mK]//g" tunnel.log 2>/dev/null | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1)"
      [ -n "$NU" ] && printf "%s\n" "$NU" > public_url.txt
    fi
  fi
  sleep 8
done
' </dev/null > watchdog.log 2>&1 &
  echo $! > "$DIR/.watchdog"
  disown 2>/dev/null || true

  # url.sh helper
  cat > "$DIR/url.sh" <<'EOF'
#!/usr/bin/env bash
DIR="${TWINMIND_DIR:-/opt/twinmind-api}"
u=""
[ -s "$DIR/public_url.txt" ] && u="$(head -1 "$DIR/public_url.txt" 2>/dev/null)"
if [ -z "$u" ] && [ -s "$DIR/tunnel.log" ]; then
  u="$(sed -r 's/\x1B\[[0-9;]*[mK]//g' "$DIR/tunnel.log" 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)"
fi
echo "$u"
EOF
  chmod +x "$DIR/url.sh"

  # reboot persistence via cron
  ( crontab -l 2>/dev/null | grep -v 'twinmind-api/vps-install.sh' ; \
    echo "@reboot TWINMIND_DIR=$DIR TWINMIND_PORT=$PORT TWINMIND_POOL_SIZE=$POOL TWINMIND_TUNNEL_TOKEN='$TOKEN' NO_TUNNEL=$NO_TUNNEL bash $DIR/vps-install.sh >/dev/null 2>&1" ) | crontab - 2>/dev/null && c_ok "added @reboot cron entry"

  c_in "waiting for services…"
  H=""; for i in $(seq 1 20); do H="$(curl -s -m 5 http://127.0.0.1:$PORT/health || true)"; [ -n "$H" ] && break; sleep 2; done
  echo "health : ${H:-down}"
fi

# ------------------------------------------------------------------- finale ----
step_title "Waiting for your public Cloudflare URL"
# Wait (briefly) for the quick-tunnel URL to appear so it is always printed.
URL=""
F='|/-\'; K=0
for i in $(seq 1 20); do
  [ -s "$DIR/public_url.txt" ] && URL="$(head -1 "$DIR/public_url.txt" 2>/dev/null)"
  if [ -z "$URL" ] && [ -s "$DIR/tunnel.log" ]; then
    URL="$(sed -r 's/\x1B\[[0-9;]*[mK]//g' "$DIR/tunnel.log" 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)"
  fi
  [ -n "$URL" ] && break
  [ "$NO_TUNNEL" = "1" ] && break
  [ -n "$TOKEN" ] && break
  K=$(( (K+1) % 4 ))
  [ -t 1 ] && printf "\r   \033[1;33m%s\033[0m registering the Cloudflare tunnel…" "${F:$K:1}"
  sleep 2
done
[ -t 1 ] && printf "\r\033[K"

echo ""
echo "============================================================"
if [ "$NO_TUNNEL" = "1" ]; then
  c_ok "TwinMind is running WITHOUT a Cloudflare tunnel."
  echo "    Use this server's own public IP/domain on port $PORT."
  echo "    Local:  http://127.0.0.1:$PORT/admin"
elif [ -n "$TOKEN" ]; then
  c_ok "Named tunnel running — URL is STABLE (your Cloudflare hostname)."
  echo "    add '/agent' for the Claude Code studio, '/admin' for the dashboard, '/v1' for OpenAI clients."
elif [ -n "$URL" ]; then
  c_ok "CLAUDE CODE STUDIO:   $URL/agent"
  c_ok "PUBLIC BASE (OpenAI): $URL/v1"
  c_ok "ADMIN DASHBOARD:      $URL/admin"
  c_ok "MODELS:               $URL/v1/models"
else
  c_wn "Tunnel URL not visible yet. Check: tail -n 30 $DIR/tunnel.log"
fi
echo "------------------------------------------------------------"
echo "current URL anytime : bash $DIR/url.sh"
echo "logs                : tail -f $DIR/server.log  |  tail -f $DIR/tunnel.log"
if [ "$USE_SYSTEMD" = "1" ]; then
  echo "services            : systemctl status twinmind.service twinmind-tunnel.service"
  echo "restart             : systemctl restart twinmind.service"
fi
echo "uninstall           : bash $DIR/vps-install.sh --uninstall"
echo "============================================================"
c_ok "Done."
