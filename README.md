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

---

## 🧑‍💻 Claude Code Studio (agent UI) — `/agent`

A Claude-Code style web UI that runs the **real Claude Code CLI** on the host with
**full root access and no permission prompts**, plus a normal OpenAI-compatible
chat engine and your own custom providers.

Open **`/agent`** (e.g. `http://127.0.0.1:8080/agent`, or your public URL + `/agent`).

### What it does

| Feature | Detail |
|---|---|
| **Claude Code engine** | Drives the real `claude` CLI headless (`-p --output-format stream-json`). Full shell, file read/write, search, web — **no permission prompts** (`IS_SANDBOX=1` + `--dangerously-skip-permissions`, works as root). |
| **Live tool trace** | Every command Claude runs, every file it writes, and every tool result is streamed into the chat and shown inline. |
| **Per-session workspace** | Each chat gets its own folder `data/workspaces/<sid>/`. Files the agent creates appear instantly in the **Files** panel. |
| **File viewer / editor** | Open any workspace file, read it, edit and save it from the UI. |
| **File upload** | Attach files to a message; they are saved in the session workspace and referenced for the agent. |
| **Multiple sessions** | Unlimited chats, each with its own workspace, model, engine and a **resumable Claude Code session** (`--session-id` / `--resume`). |
| **Search** | Full-text search across all chats and all workspace files. |
| **Model selection** | Claude Code models (`default` / `opus` / `sonnet` / `haiku`), all TwinMind gateway models, and any custom provider model. |
| **Custom providers** | Add any OpenAI-compatible (`/chat/completions`) or Anthropic-compatible (`/v1/messages`) endpoint: **base URL + API key + models**. Stored masked; keys never echoed back. |
| **Autonomous mode** | Runs like an agent — it does **not** ask for command permission, across messages and sessions. |

### 1. Install Claude Code (required for the Claude Code engine)

```bash
npm install -g @anthropic-ai/claude-code@latest
claude --version     # prints e.g. 2.1.289 (Claude Code)
```

The UI's top bar shows `claude ✓` when the CLI is detected at `/agent/api/status`.

### 2. Give Claude Code credentials

Pick **one**:

**A. Anthropic API key (simplest, works headless/root)**
```bash
export ANTHROPIC_API_KEY='sk-ant-...'
```

**B. Custom Anthropic-compatible endpoint** — add it in the UI under **Providers**
(kind = *Anthropic-compatible*). The base URL + key are passed to the CLI as
`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` for that session only.

**C. Existing Claude subscription login**
```bash
claude auth login --claudeai    # or --console for API billing
```

### 3. Use it

- **+ New chat** → describe a task. Claude Code runs commands, creates files, and
the workspace panel shows everything live.
- **Model menu** → pick the engine (Claude Code vs chat-only), a Claude Code model
  alias, a TwinMind model, or a custom provider model.
- **Providers** → register base URL + API key + models for any other service.
- **Setup** → health: CLI present, key present, gateway base, provider count.

### Agent API (for automation)

| Method | Path | Purpose |
|---|---|---|
| GET | `/agent/api/status` | CLI installed, key present, gateway base |
| GET/POST | `/agent/api/sessions` | list / create chats |
| GET/PATCH/DELETE | `/agent/api/sessions/{sid}` | fetch (with messages) / update / delete |
| POST | `/agent/api/sessions/{sid}/chat` | **SSE** chat stream (engine + model + provider) |
| GET | `/agent/api/sessions/{sid}/files` | workspace listing |
| GET/POST/DELETE | `/agent/api/sessions/{sid}/file` | read / write / delete a file |
| POST | `/agent/api/sessions/{sid}/upload` | multipart file upload |
| GET | `/agent/api/sessions/{sid}/search?q=` | search one chat + files |
| GET | `/agent/api/search?q=` | search all chats |
| GET/POST | `/agent/api/providers` | list / add custom providers |
| PATCH/DELETE | `/agent/api/providers/{id}` | update / remove |
| GET | `/agent/api/models` | TwinMind gateway models + custom provider models |

SSE event types: `start`, `init`, `text`, `thinking`, `tool_use`, `tool_result`,
`file`, `result`, `error`, `done`.

### New env vars

| Variable | Default | Meaning |
|---|---|---|
| `TWINMIND_CLAUDE_BIN` | `claude` | path to the Claude Code CLI |
| `TWINMIND_DATA_DIR` | `./data` | sessions, workspaces, providers store |
| `TWINMIND_MAX_UPLOAD` | `26214400` | max upload size (bytes, 25 MB) |
| `ANTHROPIC_API_KEY` | (empty) | Claude Code credential (or use a provider) |
| `ANTHROPIC_BASE_URL` | (empty) | override Claude Code endpoint |

> Security: the agent runs with full root and no permission prompts **by design**, as
> requested. Only expose `/agent` on hosts you control; set `TWINMIND_ADMIN_KEY` and/or
> put it behind an authenticated reverse proxy if the URL is public.

---

## 🪄 Zero-setup Claude Code on TwinMind models (no Anthropic key)

The `/agent` studio runs the **real Claude Code CLI**, but you do **not** need an
Anthropic key. When no key is present, Claude Code is pointed at the built-in
**TwinMind bridge**, which translates Anthropic's Messages API into TwinMind
chats and emulates tool calls, so Claude Code's Bash/Read/Write/Edit tools run on
TwinMind models.

```
Claude Code CLI  -->  /anthropic/v1/messages  -->  TwinMind (api2.twinmind.com)
   (agent loop)          (bridge: tool emulation)         models
```

- Default bridge model: `claude-sonnet-5` (most reliable at tool calls).
- Override with `TWINMIND_AGENT_MODEL` (e.g. `gemini-3.7-flash`).
- Add a real key (`ANTHROPIC_API_KEY`) or an Anthropic-compatible provider and
the bridge is bypassed automatically.

| Variable | Default | Meaning |
|---|---|---|
| `TWINMIND_AGENT_MODEL` | `claude-sonnet-5` | TwinMind model the bridge uses |
| `TWINMIND_BRIDGE_RETRIES` | `7` | retries when TwinMind refuses tool use |
| `TWINMIND_BRIDGE_URL` | `http://127.0.0.1:<port>/anthropic` | bridge base URL |
| `TWINMIND_BRIDGE_SYSTEM_BUDGET` | `1800` | max system-prompt chars kept |
| `TWINMIND_BRIDGE_DEBUG` | (off) | dump the last bridge request to `/tmp` |

> TwinMind runs its own companion persona and refuses coding/tool use on some
> attempts; the bridge retries with a minimal prompt until a tool call comes
> back (measured ~60% per attempt, ~99.9% within 7 attempts).

### Fully automatic start - one command

```bash
./run.sh
```

`run.sh` does everything: creates the venv, installs requirements, installs
Node.js + Claude Code CLI if missing, and starts the server. No manual `pip`,
`npm`, `export` or key steps. Colab (`colab.sh`) and the VPS installer
(`vps-install.sh`) do the same automatically.

### Mobile UI + workspace button

The studio is mobile-first:

- a **floating folder button** (bottom-right) always opens the workspace files
  panel, with a live badge showing the file count;
- the files panel and chat list become slide-in overlays with a backdrop;
- larger touch targets, 16px inputs (no iOS zoom), and safe-area padding.
