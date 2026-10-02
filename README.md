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
- **Cloudflare Tunnel**: expose it publicly with a `trycloudflare.com` URL

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
nohup cloudflared tunnel --url http://localhost:8080 > tunnel.log 2>&1 &
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

### New tunnel URL (URLs rotate on restart)
```bash
pkill -f cloudflared; sleep 2
cd ~/twinmind-api
nohup cloudflared tunnel --url http://localhost:8080 > tunnel.log 2>&1 &
sleep 12 && grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' tunnel.log | head -1
```

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
