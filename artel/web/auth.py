"""Remote-mode authentication for the dashboard.

One access token per installation, generated with `secrets`, shown once and stored only as a salted scrypt
hash in ARTEL_HOME/web_auth.json. Entering it on the login page creates a server-side session (a random id in
an HttpOnly, SameSite=Strict cookie; only its SHA-256 is stored). Rotating the token revokes every session.
Failed logins are rate-limited per client address. The token itself is never written anywhere or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import posixpath
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

COOKIE = "artel_session"
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
MAX_FAILS = 5                # failed logins per client address...
FAIL_WINDOW_S = 600          # ...within this window, then 429 until the oldest failure ages out
PREVIEW_TTL_S = 600         # how long a preview token stays valid
MAX_TRACKED = 10_000         # client addresses remembered for rate limiting (oldest dropped first)
_SCRYPT_SLOTS = threading.BoundedSemaphore(4)  # ~16 MiB each: a flood of logins cannot exhaust memory


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"too many failed logins, retry in {retry_after}s")
        self.retry_after = retry_after


def _hash_token(token: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(token.encode(), salt=salt, n=params["n"], r=params["r"], p=params["p"], dklen=32)


def _sid_key(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


class Auth:
    def __init__(self, home: Path | None = None, session_days: float = 30, clock=time.time):
        if home is None:
            from ..project import artel_home

            home = artel_home()
        self.path = Path(home) / "web_auth.json"
        self.ttl = float(session_days) * 86400
        self.clock = clock
        self.lock = threading.Lock()
        self.fails: dict[str, deque] = defaultdict(deque)
        self._data: dict = {}
        self._mtime: tuple | None = None

    # --- storage -----------------------------------------------------------
    def _load(self) -> dict:
        """Re-read when the file changed, so `artel web token --new` in another process takes effect at once."""
        try:
            st = self.path.stat()
            m = (st.st_mtime_ns, st.st_ino, st.st_size)
        except FileNotFoundError:
            self._data, self._mtime = {}, None
            return self._data
        if m != self._mtime:
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._data = {}
            self._mtime = m
        return self._data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)
        st = self.path.stat()
        self._data, self._mtime = data, (st.st_mtime_ns, st.st_ino, st.st_size)

    # --- token -------------------------------------------------------------
    def has_token(self) -> bool:
        with self.lock:
            return bool(self._load().get("token"))

    def rotate(self) -> str:
        """Issue a new token (returned once, never stored) and revoke every session."""
        token = secrets.token_urlsafe(24)
        salt = secrets.token_bytes(16)
        with self.lock:
            self._save({"token": {"algo": "scrypt", **SCRYPT, "salt": salt.hex(),
                                  "hash": _hash_token(token, salt, SCRYPT).hex(), "created": self.clock()},
                        "sessions": {}})
        return token

    def ensure_token(self) -> str | None:
        """A new token when none exists yet (to show once), else None."""
        return None if self.has_token() else self.rotate()

    def check_token(self, token: str) -> bool:
        with self.lock:
            rec = self._load().get("token")
        if not rec or not isinstance(token, str) or not token:
            return False
        got = _hash_token(token, bytes.fromhex(rec["salt"]), rec)
        return hmac.compare_digest(got, bytes.fromhex(rec["hash"]))

    # --- preview tokens ----------------------------------------------------
    def _preview_key(self) -> bytes | None:
        with self.lock:
            rec = self._load().get("token")
        # derived from the stored token hash: server-side only, and a token rotation revokes every preview token
        return hmac.new(bytes.fromhex(rec["hash"]), b"artel-preview-v1", hashlib.sha256).digest() if rec else None

    def _preview_sig(self, slug: str, directory: str, exp: int) -> str | None:
        key = self._preview_key()
        return hmac.new(key, f"{slug}\n{directory}\n{exp}".encode(), hashlib.sha256).hexdigest() if key else None

    def preview_token(self, slug: str, rel: str) -> str:
        """`<expiry>.<signature>` letting a sandboxed preview frame (which sends no cookie) fetch the files next to
        `rel` for PREVIEW_TTL seconds. Scoped to the project and the directory of `rel`."""
        exp = int(self.clock()) + PREVIEW_TTL_S
        return f"{exp}.{self._preview_sig(slug, rel.rpartition('/')[0], exp)}"

    def preview_ok(self, token: str, slug: str, rel: str) -> bool:
        """True when `token` is unexpired, genuine, for `slug`, and `rel` lies in the directory it was issued for."""
        try:
            exp_s, sig = token.split(".")
            exp = int(exp_s)
        except ValueError:
            return False
        norm = posixpath.normpath(rel)
        if exp < self.clock() or norm.startswith("..") or norm.startswith("/") or "\0" in rel:
            return False
        parts = norm.split("/")
        # the issued directory is any ancestor of the requested file; verify against each (signature decides)
        for i in range(len(parts)):
            want = self._preview_sig(slug, "/".join(parts[:i]), exp)
            if want and hmac.compare_digest(want, sig):
                return True
        return False

    # --- login and sessions ------------------------------------------------
    def _fails(self, ip: str) -> deque:
        q = self.fails[ip]
        now = self.clock()
        while q and now - q[0] > FAIL_WINDOW_S:
            q.popleft()
        return q

    def login(self, token: str, ip: str) -> str | None:
        """A new session id on success, None on a wrong token; RateLimited after too many failures."""
        with self.lock:
            q = self._fails(ip)
            if len(q) >= MAX_FAILS:
                raise RateLimited(int(FAIL_WINDOW_S - (self.clock() - q[0])) + 1)
            # counted as a failure before hashing: parallel attempts cannot all pass the check above
            q.append(self.clock())
            while len(self.fails) > MAX_TRACKED:
                self.fails.pop(next(iter(self.fails)))
        if not _SCRYPT_SLOTS.acquire(timeout=30):
            raise RateLimited(5)
        try:
            ok = self.check_token(token)
        finally:
            _SCRYPT_SLOTS.release()
        if not ok:
            return None
        sid = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            data = self._load()
            sessions = {k: v for k, v in (data.get("sessions") or {}).items() if v.get("expires", 0) > now}
            sessions[_sid_key(sid)] = {"created": now, "expires": now + self.ttl}
            self._save({**data, "sessions": sessions})
            self.fails.pop(ip, None)
        return sid

    def valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        key = _sid_key(sid)
        with self.lock:
            sessions = self._load().get("sessions") or {}
            # dict lookup by a hash of the id: no timing signal about stored ids
            rec = sessions.get(key)
            return bool(rec) and rec.get("expires", 0) > self.clock()

    def logout(self, sid: str | None) -> None:
        if not sid:
            return
        with self.lock:
            data = self._load()
            sessions = dict(data.get("sessions") or {})
            if sessions.pop(_sid_key(sid), None) is not None:
                self._save({**data, "sessions": sessions})


def cookie_value(header: str | None) -> str | None:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and v:
            return v
    return None
