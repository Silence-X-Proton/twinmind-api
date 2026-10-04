#!/usr/bin/env bash
# Cloudflare Tunnel launcher for the TwinMind OpenAI-compatible server.
#
# Usage:
#   ./tunnel.sh quick                 -> random https://*.trycloudflare.com URL (no account)
#   ./tunnel.sh token <TUNNEL_TOKEN>  -> named tunnel via token (dashboard-managed)
#   ./tunnel.sh install               -> install cloudflared if missing
#
# NOTE: Many restricted networks block Cloudflare's QUIC (UDP:7844).
#       We force --protocol http2 (TCP:443), which works everywhere.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${TWINMIND_PORT:-8080}"
# Protocol to use for the tunnel edge connection. http2 = TCP, always works.
PROTO="${TWINMIND_TUNNEL_PROTOCOL:-http2}"

install_cf() {
  if command -v cloudflared >/dev/null 2>&1; then return; fi
  echo "[*] installing cloudflared..."
  arch=$(uname -m)
  case "$arch" in
    x86_64|amd64) pkg=cloudflared-linux-amd64 ;;
    aarch64|arm64) pkg=cloudflared-linux-arm64 ;;
    *) echo "unsupported arch $arch"; exit 1 ;;
  esac
  # /usr/local/bin may not exist on all distros
  mkdir -p /usr/local/bin 2>/dev/null || true
  if [ -w /usr/local/bin ]; then
    dest=/usr/local/bin/cloudflared
  else
    dest="$PWD/cloudflared"
  fi
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/${pkg}" -o "$dest"
  chmod +x "$dest"
  if [ "$dest" != "/usr/local/bin/cloudflared" ] && [ -w /usr/local/bin ]; then cp "$dest" /usr/local/bin/cloudflared; fi
  "$dest" --version
}

cmd="${1:-quick}"
install_cf

case "$cmd" in
  install) echo "[+] cloudflared ready" ;;
  quick)
    echo "[*] starting quick tunnel -> http://localhost:${PORT} (protocol=${PROTO})"
    exec cloudflared tunnel --url "http://localhost:${PORT}" \
      --protocol "$PROTO" --no-autoupdate 2>&1 | tee tunnel.log
    ;;
  token)
    token="${2:-${TUNNEL_TOKEN:-}}"
    [ -n "$token" ] || { echo "usage: ./tunnel.sh token <TUNNEL_TOKEN>"; exit 1; }
    exec cloudflared tunnel --no-autoupdate run --token "$token" 2>&1 | tee tunnel.log
    ;;
  *) echo "usage: ./tunnel.sh [quick|token <TOKEN>|install]" ;;
esac
