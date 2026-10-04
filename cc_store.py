#!/usr/bin/env python3
"""Storage for the Claude-Code style agent UI.

Keeps, under TWINMIND_DATA_DIR (default ./data):
  sessions/<sid>.json     session metadata (title, engine, model, claude_session_id)
  sessions/<sid>.jsonl    append-only message log
  workspaces/<sid>/       per-session file workspace (created by the agent / uploads)
  providers.json          user-defined OpenAI/Anthropic compatible providers
"""
from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("TWINMIND_DATA_DIR", os.path.join(HERE, "data"))
SESS_DIR = os.path.join(DATA_DIR, "sessions")
WS_DIR = os.path.join(DATA_DIR, "workspaces")
PROVIDERS_FILE = os.path.join(DATA_DIR, "providers.json")

_lock = threading.RLock()


def _now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _ensure() -> None:
    os.makedirs(SESS_DIR, exist_ok=True)
    os.makedirs(WS_DIR, exist_ok=True)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _validate_sid(sid: str) -> str:
    """Accept existing generated IDs and safe legacy names; never sanitize IDs."""
    if (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", sid)
            or sid in (".", "..")):
        raise ValueError("invalid session id")
    return sid


def _sess_path(sid: str) -> str:
    return _safe_join(SESS_DIR, f"{_validate_sid(sid)}.json")


def _msg_path(sid: str) -> str:
    return _safe_join(SESS_DIR, f"{_validate_sid(sid)}.jsonl")


def workspace_dir(sid: str) -> str:
    d = _safe_join(WS_DIR, _validate_sid(sid))
    _ensure()
    os.makedirs(d, exist_ok=True)
    return d


def _safe_join(base: str, rel: str) -> str:
    """Reject absolute paths, parent components and existing symlink escapes.

    Return the lexical path so exclusive creation also treats an existing
    in-workspace symlink as a collision rather than creating its target.
    This is not a sandbox against concurrent hostile symlink replacement.
    """
    if (not isinstance(rel, str) or chr(0) in rel or "\\" in rel
            or os.path.isabs(rel) or ntpath.splitdrive(rel)[0]
            or ".." in rel.split("/")):
        raise ValueError("invalid workspace path")
    rb = os.path.realpath(base)
    full = os.path.abspath(os.path.join(rb, rel))
    if os.path.commonpath((rb, os.path.realpath(full))) != rb:
        raise ValueError("path escapes workspace")
    return full


# --------------------------------------------------------------------------- #
# Sessions                                                                     #
# --------------------------------------------------------------------------- #
def list_sessions() -> list[dict]:
    _ensure()
    out: list[dict] = []
    for name in os.listdir(SESS_DIR):
        if not name.endswith(".json"):
            continue
        meta = _read_json(os.path.join(SESS_DIR, name), None)
        if isinstance(meta, dict):
            out.append(meta)
    out.sort(key=lambda m: m.get("updated", 0), reverse=True)
    return out


def create_session(title: str = "", engine: str = "claude", model: str = "",
                   provider_id: str = "") -> dict:
    _ensure()
    sid = new_id("s")
    meta = {
        "id": sid,
        "title": (title or "New chat").strip()[:120] or "New chat",
        "engine": engine or "claude",
        "model": model or "",
        "provider_id": provider_id or "",
        "claude_session_id": "",
        "created": _now(),
        "updated": _now(),
        "message_count": 0,
    }
    with _lock:
        _write_json(_sess_path(sid), meta)
        workspace_dir(sid)
    return meta


def get_session(sid: str) -> Optional[dict]:
    path = _sess_path(sid)
    _ensure()
    meta = _read_json(path, None)
    return meta if isinstance(meta, dict) else None


def update_session(sid: str, **fields: Any) -> Optional[dict]:
    with _lock:
        meta = get_session(sid)
        if not meta:
            return None
        meta.update(fields)
        meta["updated"] = _now()
        _write_json(_sess_path(sid), meta)
        return meta


def delete_session(sid: str) -> bool:
    with _lock:
        meta = get_session(sid)
        if not meta:
            return False
        for p in (_sess_path(sid), _msg_path(sid)):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        shutil.rmtree(workspace_dir(sid), ignore_errors=True)
        return True


# --------------------------------------------------------------------------- #
# Messages                                                                     #
# --------------------------------------------------------------------------- #
def add_message(sid: str, role: str, content: str, meta: Optional[dict] = None) -> dict:
    path = _msg_path(sid)
    _ensure()
    msg = {
        "id": new_id("m"),
        "role": role,
        "content": content or "",
        "ts": _now(),
        "meta": meta or {},
    }
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        s = get_session(sid)
        if s:
            s["message_count"] = int(s.get("message_count", 0)) + 1
            s["updated"] = _now()
            _write_json(_sess_path(sid), s)
    return msg


def get_messages(sid: str) -> list[dict]:
    path = _msg_path(sid)
    _ensure()
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


# --------------------------------------------------------------------------- #
# Workspace files                                                              #
# --------------------------------------------------------------------------- #
def list_files(sid: str) -> list[dict]:
    base = workspace_dir(sid)
    out: list[dict] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv")]
        for d in dirs:
            full = os.path.join(root, d)
            out.append({"path": os.path.relpath(full, base), "is_dir": True,
                        "size": 0, "mtime": os.path.getmtime(full)})
        for fn in files:
            full = os.path.join(root, fn)
            try:
                size = os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            out.append({"path": os.path.relpath(full, base), "is_dir": False,
                        "size": size, "mtime": mtime})
    out.sort(key=lambda x: (not x["is_dir"], x["path"].lower()))
    return out


def read_file(sid: str, rel: str, max_bytes: int = 400_000) -> dict:
    base = workspace_dir(sid)
    try:
        full = _safe_join(base, rel)
    except ValueError:
        return {"ok": False, "error": "invalid path", "path": rel}
    if not os.path.isfile(full):
        return {"ok": False, "error": "not found", "path": rel}
    size = os.path.getsize(full)
    try:
        with open(full, "rb") as f:
            raw = f.read(max_bytes)
    except OSError as e:
        return {"ok": False, "error": str(e), "path": rel}
    try:
        text = raw.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
        binary = True
    return {"ok": True, "path": rel, "size": size, "truncated": size > max_bytes,
            "binary": binary, "content": text}


def _write_workspace_file(base: str, rel: str, data: bytes, *, overwrite: bool = False) -> dict:
    """Reserve name, name_1, ... atomically; only explicit overwrite truncates."""
    full = _safe_join(base, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    stem, ext = os.path.splitext(rel)
    index = 0
    while True:
        candidate = rel if index == 0 else f"{stem}_{index}{ext}"
        full = _safe_join(base, candidate)
        try:
            f = open(full, "wb" if overwrite else "xb")
        except FileExistsError:
            if overwrite:
                raise
            index += 1
            continue
        with f:
            f.write(data)
        return {"ok": True, "path": os.path.relpath(full, base), "size": len(data)}


def write_file(sid: str, rel: str, content: str, *, overwrite: bool = False) -> dict:
    """Write UTF-8 text, allocating a suffix unless overwrite=True is explicit."""
    return _write_workspace_file(workspace_dir(sid), rel, (content or "").encode("utf-8"),
                                 overwrite=overwrite)


def save_upload(sid: str, filename: str, data: bytes, sub: str = "uploads") -> dict:
    base = workspace_dir(sid)
    safe = os.path.basename(filename or "upload.bin")
    rel = os.path.join(sub, safe) if sub else safe
    return _write_workspace_file(base, rel, data)


def delete_file(sid: str, rel: str) -> bool:
    base = workspace_dir(sid)
    try:
        full = _safe_join(base, rel)
    except ValueError:
        return False
    try:
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            os.remove(full)
        return True
    except FileNotFoundError:
        return False


# --------------------------------------------------------------------------- #
# Search (messages + workspace files)                                          #
# --------------------------------------------------------------------------- #
def _snippet(text: str, ql: str, width: int = 90) -> str:
    i = text.lower().find(ql)
    if i < 0:
        return text[:width]
    start = max(0, i - width // 2)
    end = min(len(text), start + width)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def search(sid: str, query: str, limit: int = 50) -> dict:
    _validate_sid(sid)
    q = (query or "").strip()
    if not q:
        return {"messages": [], "files": []}
    ql = q.lower()
    msg_hits: list[dict] = []
    for m in reversed(get_messages(sid)):
        if ql in (m.get("content") or "").lower():
            msg_hits.append({"id": m.get("id"), "role": m.get("role"),
                             "snippet": _snippet(m.get("content") or "", ql), "ts": m.get("ts")})
            if len(msg_hits) >= limit:
                break
    file_hits: list[dict] = []
    for f in list_files(sid):
        if f["is_dir"]:
            continue
        name_hit = ql in f["path"].lower()
        content_hit = False
        snippet = ""
        if not name_hit and f["size"] <= 200_000:
            r = read_file(sid, f["path"], max_bytes=200_000)
            if r.get("ok") and not r.get("binary") and ql in (r.get("content") or "").lower():
                content_hit = True
                snippet = _snippet(r.get("content") or "", ql)
        if name_hit or content_hit:
            file_hits.append({"path": f["path"], "size": f["size"],
                              "name_match": name_hit, "snippet": snippet})
        if len(file_hits) >= limit:
            break
    return {"messages": msg_hits, "files": file_hits}


# --------------------------------------------------------------------------- #
# Custom providers                                                             #
# --------------------------------------------------------------------------- #
def list_providers() -> list[dict]:
    _ensure()
    data = _read_json(PROVIDERS_FILE, [])
    return data if isinstance(data, list) else []


def add_provider(name: str, base_url: str, api_key: str, models: list[str],
                 kind: str = "openai") -> dict:
    _ensure()
    with _lock:
        data = list_providers()
        prov = {
            "id": new_id("p"),
            "name": (name or "Provider").strip(),
            "base_url": (base_url or "").strip().rstrip("/"),
            "api_key": (api_key or "").strip(),
            "models": [m.strip() for m in (models or []) if m and m.strip()],
            "kind": kind if kind in ("openai", "anthropic") else "openai",
            "created": _now(),
        }
        data.append(prov)
        _write_json(PROVIDERS_FILE, data)
    return prov


def update_provider(pid: str, **fields: Any) -> Optional[dict]:
    with _lock:
        data = list_providers()
        out = None
        for p in data:
            if p.get("id") == pid:
                for k in ("name", "base_url", "api_key", "models", "kind"):
                    if k in fields and fields[k] is not None:
                        p[k] = fields[k]
                if isinstance(p.get("base_url"), str):
                    p["base_url"] = p["base_url"].strip().rstrip("/")
                out = p
                break
        _write_json(PROVIDERS_FILE, data)
    return out


def delete_provider(pid: str) -> bool:
    with _lock:
        data = list_providers()
        new = [p for p in data if p.get("id") != pid]
        if len(new) == len(data):
            return False
        _write_json(PROVIDERS_FILE, new)
    return True


def get_provider(pid: str) -> Optional[dict]:
    for p in list_providers():
        if p.get("id") == pid:
            return p
    return None


def mask_provider(p: dict) -> dict:
    q = dict(p)
    key = q.get("api_key") or ""
    q["api_key_set"] = bool(key)
    q["api_key_masked"] = (key[:4] + "..." + key[-4:]) if len(key) > 8 else ("set" if key else "")
    q.pop("api_key", None)
    return q
