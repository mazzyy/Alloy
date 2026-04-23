"""Clerk JWT verification.

Every request with an `Authorization: Bearer <jwt>` header is verified against
Clerk's JWKS endpoint. This module is the *generated-project* counterpart to
Alloy's own `app/core/clerk.py` — it is deliberately dependency-free beyond
`pyjwt` + `httpx` so it boots cleanly in the sandbox without pulling Alloy's
full runtime.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


class ClerkPrincipal:
    """Thin value-object representing the authenticated user.

    Keeping this as a plain class (not Pydantic) because the generated project
    may or may not pull Pydantic v2 into auth-path dependencies.
    """

    __slots__ = ("sub", "org_id", "org_role", "email")

    def __init__(
        self,
        sub: str,
        org_id: str | None,
        org_role: str | None,
        email: str | None,
    ) -> None:
        self.sub = sub
        self.org_id = org_id
        self.org_role = org_role
        self.email = email


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    jwks_url = os.environ.get("CLERK_JWKS_URL")
    if not jwks_url:
        raise RuntimeError(
            "CLERK_JWKS_URL not set. Set it to your Clerk instance's JWKS URL."
        )
    # httpx isn't used directly here — PyJWKClient spawns its own fetcher —
    # but we import it above so the Dockerfile pre-installs it and the
    # subsequent JWKS fetch uses a modern TLS stack.
    _ = httpx
    return jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)


def verify_clerk_jwt(token: str) -> ClerkPrincipal:
    """Verify a Clerk-issued JWT and return the principal.

    Audience is optional (`CLERK_AUDIENCE` env var). If unset we only enforce
    signature + `exp`. This matches Clerk's default template where the token
    carries no `aud` claim unless you explicitly add one.
    """
    jwks = _jwks_client()
    signing_key = jwks.get_signing_key_from_jwt(token).key

    audience = os.environ.get("CLERK_AUDIENCE") or None
    options: dict[str, Any] = {"require": ["exp", "sub"]}
    if audience is None:
        options["verify_aud"] = False

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            options=options,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Clerk token: {exc}",
        ) from exc

    return ClerkPrincipal(
        sub=str(payload["sub"]),
        org_id=payload.get("org_id"),
        org_role=payload.get("org_role"),
        email=payload.get("email"),
    )


# FastAPI dependency for protected routes.
_bearer = HTTPBearer(auto_error=True)


def current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> ClerkPrincipal:
    return verify_clerk_jwt(creds.credentials)
