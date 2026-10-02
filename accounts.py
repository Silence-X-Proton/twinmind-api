#!/usr/bin/env python3
"""
TwinMind account pool manager.

Responsibilities:
  - Create / sign in Firebase (multi-tenant) accounts for project thirdear-ai
  - Keep idTokens fresh (auto refresh before expiry)
  - Thread-safe pooled rotation across many accounts
  - Circuit-break accounts that return auth / limit errors, auto-recover them later

This module knows nothing about HTTP serving; it only produces valid bearer tokens.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# Firebase config (public client config scraped from the TwinMind web bundle)   #
# --------------------------------------------------------------------------- #
FIREBASE_API_KEY = os.environ.get(
    "TWINMIND_FIREBASE_KEY", "AIzaSyD2Sd_NP3vA4rwvoroKqDefpXZeCMDXcIQ"
)
FIREBASE_TENANT = os.environ.get("TWINMIND_TENANT", "PRODTwinMind-dcnoy")
IDENTITY = "https://identitytoolkit.googleapis.com/v1/accounts"
SECURETOKEN = "https://securetoken.googleapis.com/v1/token"
TWINMIND_API = os.environ.get("TWINMIND_API", "https://api.twinmind.com")
DELETE_USER_PATH = os.environ.get("TWINMIND_DELETE_PATH", "/api/v2/users/delete")

# idTokens are valid 3600s; refresh a little early.
TOKEN_TTL = int(os.environ.get("TWINMIND_TOKEN_TTL", "3600"))
REFRESH_MARGIN = int(os.environ.get("TWINMIND_REFRESH_MARGIN", "300"))


class AuthError(Exception):
    pass


def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            msg = body
        raise AuthError(f"HTTP {e.code}: {msg}") from e


def firebase_signup(email: str, password: str) -> dict:
    return _post_json(
        f"{IDENTITY}:signUp?key={FIREBASE_API_KEY}",
        {
            "email": email,
            "password": password,
            "returnSecureToken": True,
            "tenantId": FIREBASE_TENANT,
        },
    )


def firebase_login(email: str, password: str) -> dict:
    return _post_json(
        f"{IDENTITY}:signInWithPassword?key={FIREBASE_API_KEY}",
        {
            "email": email,
            "password": password,
            "returnSecureToken": True,
            "tenantId": FIREBASE_TENANT,
        },
    )


def firebase_refresh(refresh_token: str) -> dict:
    data = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    req = urllib.request.Request(
        f"{SECURETOKEN}?key={FIREBASE_API_KEY}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise AuthError(f"refresh HTTP {e.code}: {body[:200]}") from e


def delete_user(token: str) -> dict:
    """Delete the TwinMind user + underlying Firebase account (burn after use).
    Returns the parsed JSON response. Never raises on non-2xx status; reports it."""
    req = urllib.request.Request(
        TWINMIND_API + DELETE_USER_PATH,
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"success": False, "status": e.code, "error": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# Account                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class Account:
    email: str
    password: str
    uid: Optional[str] = None
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    token_expiry: float = 0.0
    # health / circuit breaker
    failures: int = 0
    blocked_until: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0
    total_requests: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- auth ----
    def _apply(self, payload: dict, refresh: bool = False) -> None:
        self.id_token = payload.get("id_token") or payload.get("idToken")
        self.refresh_token = payload.get("refresh_token") or payload.get(
            "refreshToken", self.refresh_token
        )
        self.uid = payload.get("user_id") or payload.get("localId", self.uid)
        self.token_expiry = time.time() + TOKEN_TTL - REFRESH_MARGIN

    def ensure_token(self) -> str:
        """Return a valid bearer token, refreshing/creating as needed."""
        with self.lock:
            if self.id_token and time.time() < self.token_expiry:
                return self.id_token
            # try refresh first
            if self.refresh_token:
                try:
                    self._apply(firebase_refresh(self.refresh_token), refresh=True)
                    return self.id_token  # type: ignore[return-value]
                except AuthError:
                    pass
            # try login
            try:
                self._apply(firebase_login(self.email, self.password))
                return self.id_token  # type: ignore[return-value]
            except AuthError:
                pass
            # last resort: signup (user may not exist)
            self._apply(firebase_signup(self.email, self.password))
            return self.id_token  # type: ignore[return-value]

    # ---- circuit breaker ----
    def mark_ok(self) -> None:
        with self.lock:
            self.failures = 0
            self.blocked_until = 0.0
            self.last_used = time.time()
            self.total_requests += 1

    def mark_fail(self, hard: bool = False, cooldown: float = 60.0) -> None:
        with self.lock:
            self.failures += 1
            if hard or self.failures >= 3:
                # exponential-ish cooldown, capped
                cd = min(cooldown * (2 ** min(self.failures - 1, 6)), 3600)
                self.blocked_until = time.time() + cd

    def available(self) -> bool:
        return time.time() >= self.blocked_until

    def snapshot(self) -> dict:
        return {
            "email": self.email,
            "uid": self.uid,
            "available": self.available(),
            "failures": self.failures,
            "blocked_until": self.blocked_until,
            "total_requests": self.total_requests,
            "last_used": self.last_used,
        }

    def burn(self) -> dict:
        """Delete this TwinMind user + Firebase account and wipe local token
        material. After burn the account leaves no trace and cannot be reused."""
        result = {"success": False}
        try:
            token = self.ensure_token()
            result = delete_user(token)
        except Exception as e:
            result = {"success": False, "error": str(e)}
        finally:
            with self.lock:
                self.id_token = None
                self.refresh_token = None
                self.token_expiry = 0.0
                self.blocked_until = float("inf")  # never reuse
        return result


# --------------------------------------------------------------------------- #
# Account pool                                                                 #
# --------------------------------------------------------------------------- #
class AccountPool:
    """Thread-safe rotating pool of TwinMind accounts."""

    def __init__(
        self,
        size: int = 5,
        domain: str = "mailinator.com",
        password: str = "Str0ng!Passw0rd123",
        state_file: Optional[str] = None,
    ) -> None:
        self.size = max(1, size)
        self.domain = domain
        self.password = password
        self.state_file = state_file
        self._accounts: list[Account] = []
        self._lock = threading.RLock()
        self._cursor = 0
        self._load_or_create()

    # ---- persistence -----------------------------------------------------
    def _load_or_create(self) -> None:
        if self.state_file and os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    raw = json.load(f)
                for a in raw.get("accounts", []):
                    acc = Account(email=a["email"], password=a.get("password", self.password))
                    acc.uid = a.get("uid")
                    acc.refresh_token = a.get("refresh_token")
                    acc.id_token = a.get("id_token")
                    acc.token_expiry = a.get("token_expiry", 0.0)
                    self._accounts.append(acc)
            except Exception:
                self._accounts = []
        while len(self._accounts) < self.size:
            self._accounts.append(self._new_account())
        self._save()

    def _new_account(self) -> Account:
        email = f"tmx{int(time.time() * 1000)}{os.getpid()}{len(self._accounts)}@{self.domain}"
        acc = Account(email=email, password=self.password)
        try:
            acc._apply(firebase_signup(email, self.password))
        except AuthError:
            # account may already exist -> login
            acc._apply(firebase_login(email, self.password))
        return acc

    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file, "w") as f:
                json.dump(
                    {
                        "accounts": [
                            {
                                "email": a.email,
                                "password": a.password,
                                "uid": a.uid,
                                "refresh_token": a.refresh_token,
                                "id_token": a.id_token,
                                "token_expiry": a.token_expiry,
                            }
                            for a in self._accounts
                        ]
                    },
                    f,
                )
        except Exception:
            pass

    # ---- rotation --------------------------------------------------------
    def acquire(self) -> Account:
        """Return the next available account (round-robin).
        If none are currently available, unblock/replace the least-recently
        blocked one and, as a last resort, mint a fresh account."""
        with self._lock:
            n = len(self._accounts)
            for _ in range(n * 2):
                acc = self._accounts[self._cursor % n]
                self._cursor += 1
                if acc.available():
                    return acc
            # all blocked -> try to revive the one with smallest cooldown left
            best = min(self._accounts, key=lambda a: a.blocked_until)
            best.blocked_until = 0.0
            best.failures = 0
            return best

    def recycle(self, acc: Account, burn: bool = True) -> Optional[Account]:
        """Replace an account with a fresh one. If `burn` is True the old
        account is deleted upstream (no trace) before being dropped."""
        with self._lock:
            try:
                if burn:
                    try:
                        acc.burn()
                    except Exception:
                        pass
                new = self._new_account()
                idx = self._accounts.index(acc)
                self._accounts[idx] = new
                self._save()
                return new
            except Exception:
                return None

    def burn_and_replace(self, acc: Account) -> Optional[Account]:
        """Explicit burn-after-use: delete the account upstream and swap in a
        fresh one, keeping the pool size constant."""
        return self.recycle(acc, burn=True)

    def rotate(self) -> Optional[Account]:
        """Burn the least-recently-used account and replace it with a fresh one.
        Useful for scheduled hygiene rotation so no account lingers."""
        with self._lock:
            if not self._accounts:
                return None
            oldest = min(self._accounts, key=lambda a: a.last_used or 0)
            return self.recycle(oldest, burn=True)

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._accounts),
                "available": sum(1 for a in self._accounts if a.available()),
                "accounts": [a.snapshot() for a in self._accounts],
            }

    def add_account(self) -> dict:
        with self._lock:
            acc = self._new_account()
            self._accounts.append(acc)
            self.size = len(self._accounts)
            self._save()
            return acc.snapshot()
