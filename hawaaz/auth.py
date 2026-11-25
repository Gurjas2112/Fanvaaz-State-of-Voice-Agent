"""Accounts, sessions and roles.

* Passwords: scrypt (stdlib), per-user salt, constant-time compare.
* Sessions: random 32-byte token in an HttpOnly, SameSite=Lax cookie; only its
  SHA-256 is stored, so a leaked database cannot be replayed as a login.
* CSRF: every state-changing request must carry `X-Requested-With: hawaaz`.
  Browsers cannot add that header cross-site without a CORS preflight, and the
  API allows no cross-origin requests.
* Brute force: 5 failed logins per email in 15 minutes -> 429.
* Roles: `admin` (users, runs, everything) and `analyst` (read, ask, triage).
* Automation: `Authorization: Bearer <HAWAAZ_API_TOKEN>` acts as an admin
  service account, for CI tools (Newman, promptfoo, Schemathesis, k6).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Settings
from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','analyst')),
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
"""

# Shared with the OpenAPI schema. Printable ASCII without "@" or space, written as explicit ranges:
# Python's \s and JSON Schema's (ECMAScript) \s disagree on \x1c-\x1f and \x85, and fuzzing found both.
EMAIL_PATTERN = r"^[!-?A-~]+@[!-?A-~]+\.[A-Za-z]{2,}$"
EMAIL_RE = re.compile(EMAIL_PATTERN)
MIN_PASSWORD = 10
COMMON_PASSWORDS = {
    "password123",
    "1234567890",
    "qwertyuiop",
    "password12",
    "iloveyou12",
    "atomberg123",
    "hawaaz1234",
    "welcome123",
    "admin12345",
    "letmein123",
}
COOKIE = "hz_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "hawaaz"


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class User:
    id: int
    email: str
    name: str
    role: str
    active: bool
    created_at: str
    last_login: str | None

    def public(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "active": self.active,
            "created_at": self.created_at,
            "last_login": self.last_login,
        }


SERVICE_USER = User(
    id=0,
    email="service@hawaaz.local",
    name="Service token",
    role="admin",
    active=True,
    created_at="",
    last_login=None,
)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(dk)),
        )
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


def _now() -> datetime:
    return datetime.now(UTC)


def _row(r) -> User:
    return User(
        id=r[0], email=r[1], name=r[2], role=r[4], active=bool(r[5]), created_at=r[6], last_login=r[7]
    )


class Accounts:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.s = store, settings
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        with store._conn() as c:
            c.executescript(SCHEMA)

    # ---- users -------------------------------------------------------------
    @staticmethod
    def _check_password(password: str, email: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AuthError(400, f"Use at least {MIN_PASSWORD} characters for the password.")
        if password.lower() in COMMON_PASSWORDS or password.lower() in {email, email.split("@")[0]}:
            raise AuthError(400, "That password is too common. Choose a longer phrase.")

    def _validate(self, email: str, password: str, name: str) -> str:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError(400, "Enter a valid email address.")
        self._check_password(password, email)
        if not name.strip():
            raise AuthError(400, "Enter your name.")
        return email

    def create_user(self, email: str, password: str, name: str, role: str = "analyst") -> User:
        email = self._validate(email, password, name)
        try:
            with self.store._conn() as c:
                c.execute(
                    "INSERT INTO users (email,name,password_hash,role,active,created_at) VALUES (?,?,?,?,1,?)",
                    (
                        email,
                        name.strip()[:80],
                        hash_password(password),
                        role,
                        _now().isoformat(timespec="seconds"),
                    ),
                )
        except Exception as e:  # sqlite3.IntegrityError
            raise AuthError(409, "An account with this email already exists. Sign in instead.") from e
        return self.get_by_email(email)  # type: ignore[return-value]

    def signup(self, email: str, password: str, name: str) -> User:
        domains = [d.strip().lower() for d in self.s.signup_domains.split(",") if d.strip()]
        if domains and email.strip().lower().rsplit("@", 1)[-1] not in domains:
            raise AuthError(403, f"Sign-up is limited to {', '.join('@' + d for d in domains)} addresses.")
        return self.create_user(email, password, name, "analyst")

    def ensure_admin(self) -> None:
        if not (self.s.admin_email and self.s.admin_password):
            return
        u = self.get_by_email(self.s.admin_email)
        if u is None:
            self.create_user(self.s.admin_email, self.s.admin_password, "Admin", "admin")
        elif u.role != "admin":
            self.update_user(u.id, role="admin", active=True)

    def get_by_email(self, email: str) -> User | None:
        with self.store._conn() as c:
            r = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        return _row(r) if r else None

    def get(self, user_id: int) -> User | None:
        with self.store._conn() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row(r) if r else None

    def list_users(self) -> list[User]:
        with self.store._conn() as c:
            return [_row(r) for r in c.execute("SELECT * FROM users ORDER BY created_at").fetchall()]

    def admin_count(self) -> int:
        with self.store._conn() as c:
            return c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        active: bool | None = None,
        password: str | None = None,
        acting: User | None = None,
    ) -> User:
        u = self.get(user_id)
        if u is None:
            raise AuthError(404, "No such user.")
        demoting = (role and role != "admin") or active is False
        if u.role == "admin" and u.active and demoting and self.admin_count() <= 1:
            raise AuthError(409, "Keep at least one active admin.")
        if acting and acting.id == user_id and demoting:
            raise AuthError(409, "You cannot remove your own admin access.")
        with self.store._conn() as c:
            if role:
                c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            if active is not None:
                c.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
                if not active:
                    c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            if password is not None:
                # Only the new password is checked: an account created under older rules must stay resettable.
                self._check_password(password, u.email)
                c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
                c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return self.get(user_id)  # type: ignore[return-value]

    # ---- sessions ----------------------------------------------------------
    def _throttled(self, email: str) -> bool:
        cutoff = time.monotonic() - 900
        with self._lock:
            fails = [t for t in self._fails.get(email, []) if t > cutoff]
            self._fails[email] = fails
            return len(fails) >= 5

    def login(self, email: str, password: str) -> tuple[User, str]:
        email = email.strip().lower()
        if self._throttled(email):
            raise AuthError(
                429, "Too many attempts. Wait 15 minutes, or ask an admin to reset your password."
            )
        u = self.get_by_email(email)
        with self.store._conn() as c:
            row = c.execute("SELECT password_hash FROM users WHERE email=?", (email,)).fetchone()
        # Same work and same message whether or not the email exists.
        ok = verify_password(password, row[0] if row else hash_password("decoy-password"))
        if not (u and ok and u.active):
            with self._lock:
                self._fails.setdefault(email, []).append(time.monotonic())
            raise AuthError(401, "Email or password is incorrect.")
        self._fails.pop(email, None)
        return self.open_session(u)

    def open_session(self, u: User) -> tuple[User, str]:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.store._conn() as c:
            c.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    u.id,
                    now.isoformat(),
                    (now + timedelta(days=self.s.session_days)).isoformat(),
                ),
            )
            c.execute("UPDATE users SET last_login=? WHERE id=?", (now.isoformat(timespec="seconds"), u.id))
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
        return self.get(u.id), token  # type: ignore[return-value]

    def logout(self, token: str) -> None:
        with self.store._conn() as c:
            c.execute(
                "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
            )

    def user_for_token(self, token: str | None) -> User | None:
        if not token:
            return None
        with self.store._conn() as c:
            r = c.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at > ?",
                (hashlib.sha256(token.encode()).hexdigest(), _now().isoformat()),
            ).fetchone()
        u = _row(r) if r else None
        return u if u and u.active else None

    def user_for_bearer(self, header: str | None) -> User | None:
        if not (header and self.s.api_token and header.startswith("Bearer ")):
            return None
        return SERVICE_USER if hmac.compare_digest(header[7:], self.s.api_token) else None
