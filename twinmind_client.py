#!/usr/bin/env python3
"""
TwinMind chat client - authenticated model chat via Firebase multi-tenant auth.

Auth chain (discovered):
  1. Firebase signUp/signInWithPassword with tenantId=PRODTwinMind-dcnoy
     POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<API_KEY>
  2. Use idToken as Bearer against backend:
       GET  https://api2.twinmind.com/api/v3/chat/models
       POST https://api2.twinmind.com/api/v3/chat   (SSE stream)
"""
import json, sys, time, urllib.request, urllib.error

FIREBASE_KEY = "AIzaSyD2Sd_NP3vA4rwvoroKqDefpXZeCMDXcIQ"
TENANT_ID    = "PRODTwinMind-dcnoy"
BACKEND      = "https://api2.twinmind.com"


def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def signup(email, password):
    return _post_json(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_KEY}",
        {"email": email, "password": password,
         "returnSecureToken": True, "tenantId": TENANT_ID})


def login(email, password):
    return _post_json(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_KEY}",
        {"email": email, "password": password,
         "returnSecureToken": True, "tenantId": TENANT_ID})


def list_models(token):
    req = urllib.request.Request(
        f"{BACKEND}/api/v3/chat/models",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def chat(token, query, model="auto", session_id=None, timeout=180):
    """Yield SSE events (dicts). model='auto' or a model_name from list_models."""
    body = {
        "type": "app", "version": 1, "response_version": 1,
        "query": query,
        "model": {"model_name": model} if model and model != "auto" else "auto",
        "context": None,
        "client": {"platform": "web", "timezone": "Asia/Kolkata",
                   "client_time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                   "locale": "en-US"},
        "mode": "default",
    }
    if session_id:
        body["session_id"] = session_id
    req = urllib.request.Request(
        f"{BACKEND}/api/v3/chat", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue


def stream_text(token, query, model="auto", session_id=None):
    """Print streamed answer; return (text, thinking, session_id, tools)."""
    text, thinking, sid, tools = "", "", session_id, []
    for ev in chat(token, query, model, session_id):
        t = ev.get("type")
        if t == "run_start":
            sid = ev.get("session_id", sid)
        elif t == "text_delta":
            text += ev.get("content", "")
            print(ev.get("content", ""), end="", flush=True)
        elif t == "thinking_delta":
            thinking += ev.get("content", "")
        elif t == "tool_call":
            tools.append(ev)
    print()
    return text, thinking, sid, tools


if __name__ == "__main__":
    email = sys.argv[2] if len(sys.argv) > 2 else f"cli{int(time.time())}@mailinator.com"
    pw = "Str0ng!Passw0rd123"
    try:
        auth = login(email, pw)
        print(f"[+] login ok: {email}", file=sys.stderr)
    except urllib.error.HTTPError:
        auth = signup(email, pw)
        print(f"[+] signup ok: {email}", file=sys.stderr)
    token = auth["idToken"]
    if len(sys.argv) > 1 and sys.argv[1] == "models":
        print(json.dumps(list_models(token), indent=2))
    else:
        q = " ".join(sys.argv[1:]) or "Hello!"
        model = "auto"
        print(f"[+] model={model} query={q!r}", file=sys.stderr)
        stream_text(token, q, model)
