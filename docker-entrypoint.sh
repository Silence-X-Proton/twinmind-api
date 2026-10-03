#!/usr/bin/env bash
# Container entrypoint for persistent hosts (Render/Fly/Railway/Koyeb/VPS).
# Starts the API, then a Cloudflare tunnel:
#   * TWINMIND_TUNNEL_TOKEN set  -> NAMED tunnel (STABLE, URL never changes)
#   * otherwise                  -> quick tunnel (random URL each start)
# On a persistent host you usually do NOT need a tunnel at all: the platform
# gives you a public HTTPS URL already (Render/Fly). Then set NO_TUNNEL=1.
set -uo pipefail

cd "$(dirname "$0")/.." 2>/dev/null || true
cd /app 2>/dev/null || true

# Many PaaS platforms inject PORT; honor it for the internal API bind.
if [ -n "${PORT:-}" ] && [ -z "${TWINMIND_PORT:-}" ]; then
  export TWINMIND_PORT="$PORT"
fi
export TWINMIND_PORT="${TWINMIND_PORT:-8080}"
export TWINMIND_HOST="${TWINMIND_HOST:-0.0.0.0}"

echo "[entrypoint] starting API on ${TWINMIND_HOST}:${TWINMIND_PORT}"
python3 app.py > server.log 2>&1 &
API_PID=$!

# Wait for API to answer locally
for i in $(seq 1 30); do
  curl -s -m 3 "http://127.0.0.1:${TWINMIND_PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done
echo "[entrypoint] local health: $(curl -s -m 5 "http://127.0.0.1:${TWINMIND_PORT}/health" || echo down)"

if [ "${NO_TUNNEL:-0}" = "1" ]; then
  echo "[entrypoint] NO_TUNNEL=1 -> using the platform's own public URL, not starting cloudflared"
  wait $API_PID
  exit $?
fi

# Start tunnel (named if token provided, else quick http2)
if [ -n "${TWINMIND_TUNNEL_TOKEN:-}" ]; then
  echo "[entrypoint] starting NAMED cloudflare tunnel (stable URL)"
  cloudflared tunnel --no-autoupdate run --token "$TWINMIND_TUNNEL_TOKEN" > tunnel.log 2>&1 &
else
  echo "[entrypoint] starting quick cloudflare tunnel (protocol=${TWINMIND_TUNNEL_PROTOCOL:-http2})"
  cloudflared tunnel --url "http://localhost:${TWINMIND_PORT}" --protocol "${TWINMIND_TUNNEL_PROTOCOL:-http2}" --no-autoupdate > tunnel.log 2>&1 &
  TUN_PID=$!
  # publish + print the URL
  for i in $(seq 1 20); do
    U="$(sed -r 's/\x1B\[[0-9;]*[mK]//g' tunnel.log 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)"
    [ -n "$U" ] && break
    sleep 3
  done
  if [ -n "${U:-}" ]; then
    printf '%s\n' "$U" > public_url.txt
    echo "[entrypoint] PUBLIC BASE: $U/v1"
    echo "[entrypoint] ADMIN:       $U/admin"
  else
    echo "[entrypoint] warning: quick-tunnel URL not found yet; check tunnel.log"
  fi
fi

# Keep the container alive if the API exits
wait $API_PID