# TwinMind Gateway — container image for persistent hosts
# (Render, Fly.io, Railway, Koyeb, a VPS, Docker, etc.)
#
# Why: Google Colab is EPHEMERAL — it recycles the VM (idle timeout / quota),
# which kills the tunnel and makes the saved URL show Cloudflare 1033/404.
# On a persistent host the process stays up and the URL stays alive.
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
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in amd64) cf=cloudflared-linux-amd64 ;; arm64) cf=cloudflared-linux-arm64 ;; *) cf=cloudflared-linux-amd64 ;; esac \
 && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/${cf}" -o /usr/local/bin/cloudflared \
 && chmod +x /usr/local/bin/cloudflared \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# entrypoint: start api, then a tunnel (named if TWINMIND_TUNNEL_TOKEN set)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

# The API binds TWINMIND_PORT; most PaaS platforms route HTTP to $PORT.
# We keep 8080 internally and let the platform map it (see render.yaml/fly.toml).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
