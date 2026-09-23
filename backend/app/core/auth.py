"""
Authentication (spec §20). Password hashing via bcrypt (called directly —
see note below), stateless JWT access + refresh tokens.

Access tokens are short-lived (ACCESS_TOKEN_EXPIRE_MINUTES) and are what
every protected endpoint actually checks. Refresh tokens are long-lived
(REFRESH_TOKEN_EXPIRE_MINUTES) and are only ever accepted by POST
/api/auth/refresh, which mints a fresh access token (and a fresh refresh
token — a sliding window) without the user having to re-enter a password.
The Android app's OkHttp Authenticator (see ApiClientFactory.kt) calls
that endpoint automatically the moment any request comes back 401, so as
long as the app keeps polling at all (which it does, continuously, for
trade execution), the session never actually expires from the user's
point of view — this is what makes "leave the bot running for a
week/month unattended" (the whole point of the app) actually hold, without
making the access token itself long-lived and therefore a much bigger
prize if the phone or the token were ever compromised.

No server-side refresh-token revocation list — still intentionally simple
for a single-operator setup. If a phone is lost, rotating
NEURAVEX_SECRET_KEY immediately invalidates every access AND refresh token
ever issued (everyone has to log in again) — that's the "kill switch" for
a compromised device today.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

# We call bcrypt directly rather than going through passlib.CryptContext.
# passlib 1.7.4 (the last release; the project is unmaintained) reads
# bcrypt.__about__.__version__ to detect the backend version, an attribute
# bcrypt removed in 4.0.0 — this raises AttributeError at hash/verify time
# on any current bcrypt install. There is no fixed passlib release to
# upgrade to. Calling bcrypt's own hashpw/checkpw avoids the broken
# version-detection path entirely.
_BCRYPT_MAX_PASSWORD_BYTES = 72  # bcrypt silently ignores bytes beyond this; truncate explicitly instead

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12  # 12h — short-lived on purpose; refresh renews it silently
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 90  # 90 days, and re-issued (sliding) on every refresh call


class AuthError(Exception):
    pass


def _require_secret_key() -> str:
    # Read os.getenv() here, at call time, rather than capturing it once as
    # a module-level global at import time. A module-level capture would
    # silently freeze in whatever value existed at the moment this module
    # was first imported — before .env may have been loaded by another
    # module — and no later load_dotenv() call would ever be seen. Reading
    # it lazily, on every call, makes this correct regardless of import
    # order anywhere else in the codebase.
    secret_key = os.getenv("NEURAVEX_SECRET_KEY", "")
    if not secret_key or secret_key == "change-me-to-a-random-64-char-string":
        raise AuthError(
            "NEURAVEX_SECRET_KEY is not set to a real secret. Refusing to issue or verify "
            "tokens with the default/placeholder key. Generate one with e.g. "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"` and set it in .env."
        )
    return secret_key


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")[:_BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    password_bytes = plain_password.encode("utf-8")[:_BCRYPT_MAX_PASSWORD_BYTES]
    return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))


def _create_token(subject: str, token_type: str, expire_minutes: int) -> str:
    secret_key = _require_secret_key()
    # str(subject): defensively coerce here, not just at each call site.
    # `subject` is meant to always be a plain string (a user id), but on a
    # long-lived database the actual Postgres column type for users.id
    # can be a native `uuid` type from an earlier schema version rather
    # than the varchar the current model declares — init_db() only
    # creates missing tables, it never alters existing ones. When that's
    # the case, SQLAlchemy/psycopg2 hand back a Python `uuid.UUID` object,
    # which `json.dumps` (used internally by jose's jwt.encode) cannot
    # serialize at all, crashing every login with a very confusing
    # "Object of type UUID is not JSON serializable" traceback that looks
    # unrelated to its actual cause. Coercing here means this can never
    # happen again regardless of what any caller passes in.
    expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    payload = {"sub": str(subject), "type": token_type, "exp": expire}
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM)


def create_access_token(subject: str) -> str:
    return _create_token(subject, "access", ACCESS_TOKEN_EXPIRE_MINUTES)


def create_refresh_token(subject: str) -> str:
    return _create_token(subject, "refresh", REFRESH_TOKEN_EXPIRE_MINUTES)


def decode_token(token: str, expected_type: str) -> str:
    """Decodes a token and enforces it's the kind the caller actually
    asked for — without this check, a leaked refresh token (which lives
    far longer) could be used directly as an access token on any
    protected endpoint, and an access token could be replayed against
    /api/auth/refresh to keep minting new sessions past its own short
    lifetime. Tokens issued before this "type" claim existed have neither
    field, so they're treated as "access" for backward compatibility —
    harmless, since no refresh tokens existed before this change either.
    """
    secret_key = _require_secret_key()
    try:
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
    except JWTError as e:
        raise AuthError(f"Invalid or expired token: {e}") from e
    subject = payload.get("sub")
    if subject is None:
        raise AuthError("Token has no subject")
    token_type = payload.get("type", "access")
    if token_type != expected_type:
        raise AuthError(f"Wrong token type: expected {expected_type}, got {token_type}")
    return subject


def decode_access_token(token: str) -> str:
    return decode_token(token, "access")
