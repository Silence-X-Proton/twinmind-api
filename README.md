# TwinMind OpenAI-Compatible API 🚀

Run a **self-hosted, OpenAI-compatible API** that talks to TwinMind's live models
(Gemini, GPT, Claude). Point any OpenAI client at it and chat — **no account needed,
no rate limits, no trace left behind**.

---

## ✨ What you get

- **OpenAI-compatible** endpoints: `/v1/models`, `/v1/chat/completions` (stream + non-stream), `/v1/completions`
- **Models**: Gemini 3.x, GPT-6.x, Claude Opus/Sonnet 5.x, plus `auto`
- **Never hits a limit**: rotating account pool + automatic retry + circuit breaker
- **Burn-after-use**: each account self-deletes (user + Firebase) after its quota — *no name, no trace*
- **Premium streaming**: long, stable SSE with heartbeats — no mid-stream break on big answers
- **Admin dashboard** at `/admin`: stats, per-model usage, request detail (in/out tokens + copy), summarize, pool resize, gateway ON/OFF, and a terminal
- **Cloudflare Tunnel**: expose it publicly with a `trycloudflare.com` URL

---

## ⚡ Google Colab — ONE command (easiest)

Open a Colab cell and paste **this single command**:

```bash
!curl -fsSL https://raw.githubusercontent.com/Silence-X-Proton/twinmind-api/main/colab.sh | bash
```

It installs everything, starts the API, opens a Cloudflare tunnel, and prints:

```
PUBLIC BASE (OpenAI):   https://xxxx.trycloudflare.com/v1
ADMIN DASHBOARD:        https://xxxx.trycloudflare.com/admin
MODELS:                 https://xxxx.trycloudflare.com/v1/models
```

Done. Use the `/v1` URL in any OpenAI client, and open `/admin` for the dashboard.

---

## 🖥️ VPS Setup (copy-paste, step by step)

> Works on any Debian/Ubuntu VPS. You do **not** need to download files manually —
the commands below clone everything from GitHub.

### 1. Clone the repo

```bash
cd ~
git clone https://github.com/Silence-X-Proton/twinmind-api.git
cd twinmind-api
```

### 2. One-time install (Python + dependencies)

```bash
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Start the API server (background)

```bash
source .venv/bin/activate
nohup python3 app.py > server.log 2>&1 &
echo "Server started. Check health:"
sleep 8
curl http://127.0.0.1:8080/health
```

You should see: `{"status":"ok","pool":{"size":15,"available":15}}`

### 4. Start Cloudflare Tunnel (get your public URL)

```bash
# install cloudflared (once)
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# start quick tunnel (background)
# IMPORTANT: --protocol http2 forces TCP:443. Colab (and many restricted
# networks) block Cloudflare QUIC (UDP:7844) which is cloudflared's default,
# causing the tunnel to silently never register -> no trycloudflare URL.
nohup cloudflared tunnel --url http://localhost:8080 --protocol http2 --no-autoupdate > tunnel.log 2>&1 &
sleep 12

# print your public URL
grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' tunnel.log | head -1
```

**Copy that `https://....trycloudflare.com` URL — that is your public API base.**
Your OpenAI base URL is that URL **+ `/v1`**

Example:
```
https://something-random.trycloudflare.com/v1
```

---

## 🔌 Use it (any OpenAI client)

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://YOUR-TUNNEL.trycloudflare.com/v1",
    api_key="anything",          # not required unless you set TWINMIND_API_KEYS
)

# list models
print([m.id for m in client.models.list().data])

# chat
r = client.chat.completions.create(
    model="gpt-6-luna",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(r.choices[0].message.content)

# streaming
for chunk in client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Count 1 to 5"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

### curl

```bash
curl https://YOUR-TUNNEL.trycloudflare.com/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hi"}]}'
```

---

## 🤖 Available models

| Provider | Models |
|---|---|
| **google** | gemini-3.1-pro-thinking, gemini-3.8-flash-thinking, gemini-3.7-flash, gemini-3.6-flash |
| **openai** | gpt-6-astra-thinking, gpt-6.1-sol-thinking, gpt-6-luna, gpt-5.6-terra |
| **anthropic** | claude-opus-5-5-thinking, claude-opus-5-thinking, claude-sonnet-5-5, claude-sonnet-5 |
| **twinmind** | auto |

OpenAI aliases auto-map: `gpt-4`→gpt-6-luna, `gpt-3.5-turbo`→gemini-3.6-flash,
`claude-3-opus`→claude-opus-5-thinking, `o1`→gpt-6-astra-thinking, etc.

---

## ⚙️ Configuration (env vars)

Set before starting `app.py`:

| Variable | Default | Meaning |
|---|---|---|
| `TWINMIND_POOL_SIZE` | `15` | number of accounts in the rotation pool |
| `TWINMIND_PORT` | `8080` | HTTP port |
| `TWINMIND_REQ_PER_ACCOUNT` | `300` | requests before an account is burned |
| `TWINMIND_ROTATE_INTERVAL` | `0` | seconds between hygiene rotations (0=off) |
| `TWINMIND_HEARTBEAT` | `15` | SSE keep-alive interval (seconds) |
| `TWINMIND_MAX_RETRIES` | `8` | retries across accounts per request |
| `TWINMIND_TIMEOUT` | `900` | upstream timeout (seconds) |
| `TWINMIND_API_KEYS` | (empty) | comma-separated keys to require from clients |
| `TWINMIND_DEFAULT_MODEL` | `auto` | model when client sends none |

Example:
```bash
TWINMIND_POOL_SIZE=25 TWINMIND_REQ_PER_ACCOUNT=1 nohup python3 app.py > server.log 2>&1 &
```

---

## 🧑‍💼 Admin endpoints

```bash
# pool stats (emails, request counts, health)
curl http://127.0.0.1:8080/admin/stats

# add one account
curl -X POST http://127.0.0.1:8080/admin/accounts/add

# burn the oldest account + replace (rotation)
curl -X POST http://127.0.0.1:8080/admin/accounts/rotate

# burn ALL accounts + refill
curl -X POST http://127.0.0.1:8080/admin/accounts/burn-all
```

---

## 🛠️ Operations

### Stop the server
```bash
pkill -f 'python3 app.py'
```

### Restart the server
```bash
pkill -f 'python3 app.py'; sleep 2
cd ~/twinmind-api && source .venv/bin/activate
nohup python3 app.py > server.log 2>&1 &
```

### Stop the tunnel
```bash
pkill -f cloudflared
```

### View logs
```bash
tail -f ~/twinmind-api/server.log
tail -f ~/twinmind-api/tunnel.log
```

### Cloudflare Error 1033 / 404 on your URL ("No web page was found")

**Cause:** a quick tunnel gets a **new random URL every time cloudflared restarts** (crash, network blip, Colab idle, or a manual re-run). Any previously saved link then points at a hostname with no tunnel behind it -> Cloudflare edge returns **1033 / 404**.

**Fix:**
1. Get the **current** live URL (never reuse an old one):
   ```bash
   bash /content/twinmind-api/url.sh          # prints the current URL
   ```
   The admin **Overview → Live public link** card also always shows the current URL (auto-refreshes).
2. Re-run the launcher: it is **idempotent**. If server **and tunnel** are healthy it reuses them (URL unchanged); if the tunnel has died it restarts the stack and prints a fresh URL:
   ```bash
   !curl -fsSL https://raw.githubusercontent.com/Silence-X-Proton/twinmind-api/main/colab.sh | bash
   ```
3. **Want a URL that never changes?** Use a Cloudflare **named tunnel**:
   ```bash
   !TWINMIND_TUNNEL_TOKEN='<your-cloudflare-tunnel-token>' bash /content/twinmind-api/colab.sh
   ```
   (Cloudflare Zero Trust → Networks → Tunnels → create → copy the connector token.) Stable URL across restarts.

### New tunnel URL (URLs rotate on restart)
```bash
pkill -f cloudflared; sleep 2
cd ~/twinmind-api
nohup cloudflared tunnel --url http://localhost:8080 --protocol http2 --no-autoupdate > tunnel.log 2>&1 &
sleep 12 && grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' tunnel.log | head -1
```

---

## 🏠 Persistent hosting (NO more dead URLs on Colab)

Google Colab is **ephemeral**: it recycles the VM on idle timeout / quota, which kills the tunnel and makes your saved URL show Cloudflare **1033/404**. If that keeps happening, run TwinMind on a **persistent host** — the process stays up 24/7 and the URL stays alive.

### Option A — Render (free tier, one-click)

1. Push this repo to GitHub (already done).
2. Render → **New → Blueprint** → pick this repo (uses `render.yaml` + `Dockerfile`).
3. Render builds the container and gives you a **permanent** `https://<name>.onrender.com` URL.
   - No Cloudflare tunnel needed (`NO_TUNNEL=1`); Render provides HTTPS.
   - `https://<name>.onrender.com/admin` is your dashboard.

### Option B — Fly.io

```bash
fly launch --no-deploy          # detects fly.toml
fly deploy
# optional stable named-tunnel URL instead of Fly's URL:
# fly secrets set TWINMIND_TUNNEL_TOKEN=<from Cloudflare Zero Trust>
```

### Option C — any VPS / Docker

```bash
docker build -t twinmind .
docker run -d --name twinmind -p 8080:8080 \
  -e TWINMIND_TUNNEL_TOKEN='<optional named tunnel token>' \
  twinmind
# without a token the container starts its own quick tunnel and prints the URL
```

### Optional: protect the endpoints

Set these env vars on any host:
- `TWINMIND_API_KEYS=sk-key1,sk-key2` — require an API key for `/v1/*`
- `TWINMIND_ADMIN_KEY=change-me` — lock the `/admin` UI

---

## 🔒 How it works (short)

1. On start, the pool creates `TWINMIND_POOL_SIZE` accounts via Firebase
   (multi-tenant, project `thirdear-ai`) and keeps their tokens fresh.
2. Each API request takes the next account (round-robin), sends the chat to
   TwinMind's backend (`api2.twinmind.com`), and streams the answer back in
   OpenAI format.
3. Any auth/limit/5xx error → the request is **retried on the next account**.
   The caller never sees a limit.
4. After `TWINMIND_REQ_PER_ACCOUNT` requests an account is **deleted upstream**
   and replaced — burn-after-use, no residue.

---

## 🖥️ Admin Dashboard (`/admin`)

Open your public URL **+ `/admin`** (mobile friendly) — or locally `http://127.0.0.1:8080/admin`.

Tabs:

| Tab | What it does |
|---|---|
| **Overview** | Total requests, input tokens, output tokens, total tokens, failed, pool status, uptime, and a **per-model** breakdown (requests + in/out tokens). |
| **Requests** | Live list of every request (time, model, in/out tokens, status, stream/sync). **Tap a row** to open full detail. |
| **Request detail** | Shows exactly what went in and what came out — **Input (by user)** and **Output (by model)** with token counts, plus **Copy** buttons and a **Summarize this request** button. |
| **Summarize** | Paste any text and summarize it with a model of your choice (e.g. `gpt-6-astra-thinking`, `claude-opus-5-5-thinking`). |
| **Pool** | **Resize** the account pool (grow/shrink), add one, rotate (burn oldest), or **burn all**. |
| **Terminal** | Run shell commands on the host right from the browser. |
| **Health** | Raw `/health` output. |

**Gateway switch (Overview tab):** press **Turn OFF** — the API immediately rejects all new
requests with `503` until you press **Turn ON**. Use this to stop serving without killing the process.

### Protect the dashboard (optional)

Set an admin password before starting:

```bash
export TWINMIND_ADMIN_KEY='your-secret'
nohup python3 app.py > server.log 2>&1 &
```

Then open `https://YOUR-URL/admin?key=your-secret` (it is stored in the browser for you).

---


---

## 🔁 Continuous running + Destroy

The Colab launcher runs a **watchdog**: if the API or the tunnel ever dies, it
automatically restarts them. So it keeps running continuously — no manual restart.

It stops **only** when:
- you press **Destroy gateway** in the admin **Health** tab (asks you **4 times**;
  burns all accounts, kills the tunnel + server), or
- the host / Colab session ends (Colab idle limit).

## ❗ Notes

- The quick-tunnel URL **changes every restart**. For a stable URL, create a
  named tunnel in the Cloudflare dashboard and run:
  `cloudflared tunnel run --token <YOUR_TOKEN>`
- Keep the server and tunnel running (they are background processes). Use
  `nohup` as shown, or run them in `tmux`/`screen`.
- `pool_state.json` is created automatically and holds the current pool so a
  restart reuses accounts instead of creating new ones.

---

## 📜 License / Use

For authorized testing and personal use. You are responsible for complying with
all applicable terms and laws in your environment.
