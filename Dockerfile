# TwinMind Gateway — container image for persistent hosts
# (Render, Fly.io, Railway, Koyeb, a VPS, Docker, etc.)
#
# Why: on a persistent host the process stays up 24/7 and the public URL stays
# alive (ephemeral notebook VMs recycle and kill the tunnel -> dead URLs).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TWINMIND_HOST=0.0.0.0 \
    TWINMIND_PORT=8080 \
    TWINMIND_POOL_SIZE=15 \
    TWINMIND_TUNNEL_PROTOCOL=http2

WORKDIR /app

# deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app
COPY . .

# cloudflared (for quick tunnel if no named-tunnel token is provided)
# plus Node.js + Claude Code CLI so the /agent studio works inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates nodejs npm \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in amd64) cf=cloudflared-linux-amd64 ;; arm64) cf=cloudflared-linux-arm64 ;; *) cf=cloudflared-linux-amd64 ;; esac \
 && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/${cf}" -o /usr/local/bin/cloudflared \
 && chmod +x /usr/local/bin/cloudflared \
 && npm install -g @anthropic-ai/claude-code@latest >/dev/null 2>&1 || true \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Root container: the studio runs Claude Code with full root and no prompts.
ENV IS_SANDBOX=1

# entrypoint: start api, then a tunnel (named if TWINMIND_TUNNEL_TOKEN set)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

# The API binds TWINMIND_PORT; most PaaS platforms route HTTP to $PORT.
# We keep 8080 internally and let the platform map it (see render.yaml/fly.toml).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
