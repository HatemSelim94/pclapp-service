"""API-key authentication.

A key is ``pk_`` + 43 URL-safe characters (32 random bytes). The database stores only its
SHA-256; the key itself is shown once, when issued. SHA-256 (not bcrypt) is deliberate:
bcrypt exists to slow down guessing of low-entropy passwords, and a 256-bit random key
cannot be guessed, so the slow hash would only cost us latency on every request.

The admin key is a single value from configuration, not a row; it can create users and
issue keys, and nothing else.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.models import ApiKey, User

KEY_PREFIX = "pk_"
_bearer = HTTPBearer(auto_error=False)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def display_prefix(key: str) -> str:
    """The part of the key a user can see in listings, e.g. ``pk_3fK9x``."""
    return key[: len(KEY_PREFIX) + 5]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid API key.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _credential(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if creds is None or not creds.credentials:
        raise _unauthorized()
    return creds.credentials


def current_user(
    key: Annotated[str, Depends(_credential)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    """Resolve the bearer key to its user; 401 if unknown or revoked."""
    row = session.scalars(
        select(ApiKey)
        .where(ApiKey.key_hash == hash_key(key), ApiKey.revoked_at.is_(None))
        .join(ApiKey.user)
    ).first()
    if row is None:
        raise _unauthorized()
    return row.user


def require_admin(
    key: Annotated[str, Depends(_credential)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not hmac.compare_digest(key, settings.admin_key.get_secret_value()):
        # 404 would hide the endpoint, but admin routes are documented; 403 is honest here.
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Admin key required.")


def issue_key(session: Session, user: User, label: str = "") -> tuple[ApiKey, str]:
    """Create a key row for ``user``; return it with the plain key (the only time it exists)."""
    key = generate_key()
    row = ApiKey(
        user_id=user.id, key_hash=hash_key(key), key_prefix=display_prefix(key), label=label
    )
    session.add(row)
    session.flush()
    return row, key


def revoke_key(session: Session, api_key: ApiKey) -> None:
    api_key.revoked_at = datetime.now(UTC)
    session.flush()


CurrentUser = Annotated[User, Depends(current_user)]
AdminOnly = Depends(require_admin)
