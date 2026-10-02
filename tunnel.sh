#!/usr/bin/env bash
# Cloudflare Tunnel launcher for the TwinMind OpenAI-compatible server.
#
# Usage:
#   ./tunnel.sh quick                 -> random https://*.trycloudflare.com URL (no account)
#   ./tunnel.sh token <TUNNEL_TOKEN>  -> named tunnel via token (dashboard-managed)
#   ./tunnel.sh install               -> install cloudflared if missing
#
set -euo pipefail
cd "$(dirname "$0")"

PORT="${TWINMIND_PORT:-8080}"

install_cf() {
  if command -v cloudflared >/dev/null 2>&1; then return; fi
  echo "[*] installing cloudflared..."
  arch=$(uname -m)
  case "$arch" in
    x86_64|amd64) pkg=cloudflared-linux-amd64 ;;
    aarch64|arm64) pkg=cloudflared-linux-arm64 ;;
    *) echo "unsupported arch $arch"; exit 1 ;;
  esac
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/${pkg}" -o /usr/local/bin/cloudflared
  chmod +x /usr/local/bin/cloudflared
  cloudflared --version
}

cmd="${1:-quick}"
install_cf

case "$cmd" in
  install) echo "[+] cloudflared ready" ;;
  quick)
    echo "[*] starting quick tunnel -> http://localhost:${PORT}"
    exec cloudflared tunnel --url "http://localhost:${PORT}" --no-autoupdate 2>&1 | tee tunnel.log
    ;;
  token)
    token="${2:-${TUNNEL_TOKEN:-}}"
    [ -n "$token" ] || { echo "usage: ./tunnel.sh token <TUNNEL_TOKEN>"; exit 1; }
    exec cloudflared tunnel --no-autoupdate run --token "$token" 2>&1 | tee tunnel.log
    ;;
  *) echo "usage: ./tunnel.sh [quick|token <TOKEN>|install]" ;;
esac
