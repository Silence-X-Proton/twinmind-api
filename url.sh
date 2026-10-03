#!/usr/bin/env bash
DIR="${TWINMIND_DIR:-/opt/twinmind-api}"
u=""
[ -s "$DIR/public_url.txt" ] && u="$(head -1 "$DIR/public_url.txt" 2>/dev/null)"
if [ -z "$u" ] && [ -s "$DIR/tunnel.log" ]; then
  u="$(sed -r 's/\x1B\[[0-9;]*[mK]//g' "$DIR/tunnel.log" 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)"
fi
echo "$u"
