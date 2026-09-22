"""
Authentication (spec §20). Password hashing via bcrypt (called directly —
see note below), stateless JWT access tokens. Kept intentionally simple —
no refresh-token rotation or OAuth here; add that before exposing this
beyond a single-operator setup.
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
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12


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


def create_access_token(subject: str) -> str:
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
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(subject), "exp": expire}
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str:
    secret_key = _require_secret_key()
    try:
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
    except JWTError as e:
        raise AuthError(f"Invalid or expired token: {e}") from e
    subject = payload.get("sub")
    if subject is None:
        raise AuthError("Token has no subject")
    return subject
