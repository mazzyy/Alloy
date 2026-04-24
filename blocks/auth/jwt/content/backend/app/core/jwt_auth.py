"""Self-hosted JWT auth for the generated app.

Issues HS256 tokens signed with `ALLOY_JWT_SECRET`. We chose HS256 over
RS256 deliberately: a generated app rarely needs a separate verifier
service, and asymmetric keys turn into a key-rotation footgun for users
who don't already run a KMS. Switch to RS256 by replacing the algorithm
+ key load below.

The `current_user` dependency resolves the bearer token to a `User`
row in the generated database. We deliberately do NOT cache the row —
per-request DB hits are negligible at this scale and caching invites
stale-membership bugs after role changes.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlmodel import Session, select

# Lazy imports for the generated project's User model and DB session
# factory — both are emitted by the scaffolder, so we resolve them at
# call time to avoid an import cycle at module load.

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ────────────────────────────────────────────────────────────────────
# Password hashing
# ────────────────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ────────────────────────────────────────────────────────────────────
# Token issue / verify
# ────────────────────────────────────────────────────────────────────


def _secret() -> str:
    secret = os.environ.get("ALLOY_JWT_SECRET")
    if not secret or len(secret) < 32:
        raise RuntimeError(
            "ALLOY_JWT_SECRET must be set to a string at least 32 bytes long. "
            "Generate one with `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`."
        )
    return secret


def _ttl_minutes() -> int:
    raw = os.environ.get("ALLOY_JWT_TTL_MINUTES", "60")
    try:
        return max(1, int(raw))
    except ValueError:
        return 60


def issue_token(user_id: UUID | str, *, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_ttl_minutes())).timestamp()),
        "iss": os.environ.get("ALLOY_JWT_ISSUER", "alloy-app"),
    }
    aud = os.environ.get("ALLOY_JWT_AUDIENCE")
    if aud:
        payload["aud"] = aud
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    options: dict[str, Any] = {"require": ["exp", "sub"]}
    aud = os.environ.get("ALLOY_JWT_AUDIENCE") or None
    if aud is None:
        options["verify_aud"] = False
    try:
        return jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            audience=aud,
            options=options,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ────────────────────────────────────────────────────────────────────
# FastAPI dependencies
# ────────────────────────────────────────────────────────────────────


def _get_session_dep() -> Any:
    """Resolve the generated project's session dep at call time.

    The scaffolder emits `app.core.db.get_session` — but we don't import
    it eagerly so `app.core.jwt_auth` stays importable in unit tests
    that don't have a real DB.
    """
    from app.core.db import get_session  # type: ignore[import-not-found]

    return get_session


def _get_user_model() -> Any:
    from app.models.user import User  # type: ignore[import-not-found]

    return User


def current_user(
    token: str = Depends(_oauth2_scheme),
    session: Session = Depends(_get_session_dep()),
) -> Any:
    """Resolve the bearer token to a `User` row.

    Returns the model instance, not a Pydantic schema — generated route
    handlers can call `.model_dump()` themselves if they want to project
    a response shape.
    """
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
        )
    UserModel = _get_user_model()
    try:
        ident: Any = UUID(user_id)
    except (TypeError, ValueError):
        ident = user_id
    user = session.exec(select(UserModel).where(UserModel.id == ident)).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject does not resolve to a user",
        )
    if getattr(user, "is_active", True) is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disabled",
        )
    return user
