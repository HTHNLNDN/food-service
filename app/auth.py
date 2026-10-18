"""Accounts and login sessions.

People create their own account (username + password) with an invite code, and a session
cookie keeps a device signed in for 90 days while it's used. The forms use standard
`autocomplete` fields, so browser and phone password managers offer to save and fill them.

What the database holds about an account, by design:
- no username: only HMAC-SHA256(USERNAME_KEY, normalized username). The key lives in the
  environment, not the database, so a leaked DB or backup doesn't reveal who has an account;
- no password: only its Argon2id hash;
- no session token: only its SHA-256.
There is no password reset, by decision: a forgotten password means a lost account.

Every route is behind `auth_middleware` by default; only OPEN_PATHS and /static are public.
"""

import hashlib
import hmac
import secrets
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlsplit

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app import telemetry
from app.db import Conn

SESSION_COOKIE = "fs_session"
PERSISTENT_SESSION = timedelta(days=90)  # "keep me signed in": renewed while in use
TRANSIENT_SESSION = timedelta(hours=12)  # unticked: browser-session cookie, hard 12 h cap
RENEW_EVERY = timedelta(days=1)  # slide a persistent session at most daily (not a write per hit)
MIN_PASSWORD_LENGTH = 8  # NIST 800-63B: length over composition rules
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 64
MIN_USERNAME_KEY_BYTES = 32

OPEN_PATHS = {"/login", "/signup"}  # everything else requires a signed-in user

_hasher = PasswordHasher()  # argon2id, RFC 9106 low-memory profile
# Verified against when the username doesn't exist, so both cases take the same time.
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


@dataclass(frozen=True)
class User:
    id: int  # there is deliberately no username: the app never knows it after sign-in


# --- usernames: normalized, then keyed-hashed ---


def parse_username_key(hex_key: str) -> bytes:
    """USERNAME_KEY from the environment; refuses a missing or weak key."""
    try:
        key = bytes.fromhex(hex_key.strip())
    except ValueError:
        key = b""
    if len(key) < MIN_USERNAME_KEY_BYTES:
        raise RuntimeError(
            f"USERNAME_KEY must be at least {MIN_USERNAME_KEY_BYTES} random bytes as hex "
            "(generate one with `openssl rand -hex 32`)")
    return key


def normalize_username(username: str) -> str:
    """The form a username is compared in: NFKC + case-folded, so "Claes", "CLAES" and
    full-width look-alikes are the same account."""
    name = unicodedata.normalize("NFKC", username).strip().casefold()
    if not MIN_USERNAME_LENGTH <= len(name) <= MAX_USERNAME_LENGTH or any(c.isspace() for c in name):
        raise ValueError(f"username must be {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} "
                         "characters, no spaces")
    return name


def username_hash(key: bytes, username: str) -> str:
    return hmac.new(key, normalize_username(username).encode(), hashlib.sha256).hexdigest()


def _lookup_hash(key: bytes, username: str) -> str | None:
    """The hash to look a login up by, or None for input that can't be a valid username."""
    try:
        return username_hash(key, username)
    except ValueError:
        return None


# --- accounts ---


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def create_user(conn: psycopg.Connection, key: bytes, username: str, password: str) -> User:
    name_hash = username_hash(key, username)
    pw_hash = _hasher.hash(validate_password(password))
    try:
        row = conn.execute(
            "INSERT INTO users (username_hash, password_hash) VALUES (%s, %s) RETURNING id",
            (name_hash, pw_hash),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise ValueError("that username is taken") from None
    return User(row["id"])


def set_password(conn: psycopg.Connection, user_id: int, password: str) -> None:
    """Change your own password and sign out everywhere (a stolen session dies with it)."""
    pw_hash = _hasher.hash(validate_password(password))
    with conn.transaction():
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (pw_hash, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def _verify(conn: psycopg.Connection, row, password: str) -> bool:
    try:
        _hasher.verify(row["password_hash"] if row else _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    if row is None:
        return False
    if _hasher.check_needs_rehash(row["password_hash"]):  # parameters were raised since
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                     (_hasher.hash(password), row["id"]))
    return True


def authenticate(conn: psycopg.Connection, key: bytes, username: str, password: str) -> User | None:
    name_hash = _lookup_hash(key, username)
    row = conn.execute("SELECT id, password_hash FROM users WHERE username_hash = %s",
                       (name_hash,)).fetchone() if name_hash else None
    return User(row["id"]) if _verify(conn, row, password) else None


def check_password(conn: psycopg.Connection, user_id: int, password: str) -> bool:
    row = conn.execute("SELECT id, password_hash FROM users WHERE id = %s", (user_id,)).fetchone()
    return _verify(conn, row, password)


# --- sessions ---


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(conn: psycopg.Connection, user_id: int, *, persistent: bool,
                  user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    lifetime = PERSISTENT_SESSION if persistent else TRANSIENT_SESSION
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, persistent, user_agent) "
        "VALUES (%s, %s, %s, %s, %s)",
        (_token_hash(token), user_id, datetime.now(UTC) + lifetime, persistent, user_agent[:300]),
    )
    return token


def session_user(conn: psycopg.Connection, token: str) -> User | None:
    """The user a session token belongs to, or None if unknown/expired. Persistent sessions
    slide forward (at most once a day) so a device that's used regularly stays signed in."""
    now = datetime.now(UTC)
    row = conn.execute(
        "SELECT user_id, persistent, last_seen_at FROM sessions "
        "WHERE token_hash = %s AND expires_at > %s",
        (_token_hash(token), now),
    ).fetchone()
    if row is None:
        return None
    if row["persistent"] and now - row["last_seen_at"] > RENEW_EVERY:
        conn.execute(
            "UPDATE sessions SET last_seen_at = %s, expires_at = %s WHERE token_hash = %s",
            (now, now + PERSISTENT_SESSION, _token_hash(token)),
        )
    return User(row["user_id"])


def end_session(conn: psycopg.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))


def set_session_cookie(response: Response, request: Request, token: str, persistent: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(PERSISTENT_SESSION.total_seconds()) if persistent else None,  # None: until browser closes
        httponly=True,  # not readable by page scripts
        samesite="lax",  # not sent on cross-site POSTs (CSRF)
        secure=request.url.scheme == "https",  # https behind Caddy; plain http only for local dev
        path="/",
    )


# --- throttling ---


class LoginLimiter:
    """At most MAX attempts per key per WINDOW; then locked for the rest of the window.
    In-process (the app runs one worker), so it resets on restart."""

    def __init__(self, max_attempts: int = 5, window_s: int = 15 * 60):
        self.MAX = max_attempts
        self.WINDOW = window_s
        self._attempts: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key, now):
        q = self._attempts.get(key)
        while q and now - q[0] > self.WINDOW:
            q.popleft()
        return q

    def locked(self, key) -> bool:
        with self._lock:
            q = self._recent(key, time.monotonic())
            return bool(q) and len(q) >= self.MAX

    def fail(self, key) -> None:
        with self._lock:
            self._attempts.setdefault(key, deque()).append(time.monotonic())

    def reset(self, key) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._attempts.clear()


limiter = LoginLimiter()  # failed logins per (username, IP)
signup_limiter = LoginLimiter(max_attempts=10, window_s=60 * 60)  # every sign-up try per IP


# --- request plumbing ---


def _lookup(pool, token: str) -> User | None:
    with pool.connection() as conn:
        return session_user(conn, token)


def _foreign_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return bool(origin) and origin != "null" and urlsplit(origin).netloc != request.headers.get("host")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _key(request: Request) -> bytes:
    return request.app.state.username_key


def safe_next(target: str | None) -> str:
    """Only same-site paths: never `//evil.example` or `https://…` (open redirect)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


async def auth_middleware(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and _foreign_origin(request):
        return HTMLResponse("<h1>Forbidden</h1><p>Cross-site request refused.</p>", status_code=403)
    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/static/"):
        return await call_next(request)
    token = request.cookies.get(SESSION_COOKIE)
    try:
        user = await run_in_threadpool(_lookup, request.app.state.pool, token) if token else None
    except psycopg.OperationalError:  # runs outside the app's exception handlers
        from app.main import database_unavailable
        return database_unavailable(request, None)
    if user is None:
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    request.state.user = user
    with telemetry.tenant(str(user.id)):  # attribute this request's LLM calls to the user
        return await call_next(request)


def current_user(request: Request) -> User:
    return request.state.user


CurrentUser = Annotated[User, Depends(current_user)]


def _signed_in_response(request: Request, conn, user: User, target: str, persistent: bool):
    token = start_session(conn, user.id, persistent=persistent,
                          user_agent=request.headers.get("user-agent", ""))
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, request, token, persistent)
    return response


def _already_signed_in(request: Request, conn) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token) and session_user(conn, token) is not None


# --- routes ---

router = APIRouter()


def _page(request: Request, template: str, status_code: int = 200, **ctx):
    return request.app.state.templates.TemplateResponse(
        request, template, ctx, status_code=status_code)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, conn: Conn, next: str = "/"):
    if _already_signed_in(request, conn):
        return RedirectResponse(safe_next(next), status_code=303)
    return _page(request, "login.html", title="Sign in", next=safe_next(next), error="",
                 username="", signup_open=bool(request.app.state.config.signup_code))


@router.post("/login")
def login(
    request: Request,
    conn: Conn,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    remember: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
):
    target = safe_next(next)
    signup_open = bool(request.app.state.config.signup_code)
    key = (_lookup_hash(_key(request), username) or "", _client_ip(request))
    if limiter.locked(key):
        return _page(request, "login.html", 429, title="Sign in", next=target, username=username,
                     signup_open=signup_open, error="Too many attempts. Try again in 15 minutes.")
    user = authenticate(conn, _key(request), username, password)
    if user is None:
        limiter.fail(key)
        return _page(request, "login.html", 401, title="Sign in", next=target, username=username,
                     signup_open=signup_open, error="Wrong username or password.")
    limiter.reset(key)
    return _signed_in_response(request, conn, user, target, persistent=bool(remember))


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, conn: Conn):
    if _already_signed_in(request, conn):
        return RedirectResponse("/", status_code=303)
    return _page(request, "signup.html", title="Create account", error="", username="",
                 signup_open=bool(request.app.state.config.signup_code))


@router.post("/signup")
def signup(
    request: Request,
    conn: Conn,
    invite_code: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
    understood: Annotated[str, Form()] = "",
):
    code = request.app.state.config.signup_code

    def fail(error: str, status_code: int = 400):
        return _page(request, "signup.html", status_code, title="Create account", error=error,
                     username=username, signup_open=bool(code))

    if not code:
        return fail("Sign-up is closed.", 403)
    ip_key = ("signup", _client_ip(request))
    if signup_limiter.locked(ip_key):
        return fail("Too many attempts. Try again in an hour.", 429)
    signup_limiter.fail(ip_key)  # every attempt counts, successful or not
    if not hmac.compare_digest(invite_code.strip().encode(), code.encode()):
        return fail("That invite code isn't right.", 403)
    if password != confirm_password:
        return fail("The passwords don't match.")
    if not understood:
        return fail("Please confirm you understand a forgotten password can't be recovered.")
    try:
        user = create_user(conn, _key(request), username, password)
    except ValueError as exc:
        return fail(str(exc).capitalize() + ".")
    return _signed_in_response(request, conn, user, "/", persistent=True)


@router.post("/logout")
def logout(request: Request, conn: Conn):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        end_session(conn, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, saved: bool = False):
    return _page(request, "account.html", title="Account", saved=saved, error="")


@router.post("/account/password")
def change_password(
    request: Request,
    conn: Conn,
    user: CurrentUser,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
):
    def fail(error: str, status_code: int):
        return _page(request, "account.html", status_code, title="Account", saved=False, error=error)

    if not check_password(conn, user.id, current_password):
        return fail("Current password is wrong.", 401)
    if new_password != confirm_password:
        return fail("The new passwords don't match.", 400)
    try:
        set_password(conn, user.id, new_password)  # signs out every device…
    except ValueError as exc:
        return fail(str(exc).capitalize() + ".", 400)
    response = _signed_in_response(request, conn, user, "/account?saved=1", persistent=True)
    return response  # …except this one
