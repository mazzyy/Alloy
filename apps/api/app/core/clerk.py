"""Clerk JWT verification.

Clerk issues RS256-signed JWTs whose signing keys are published at a JWKS
endpoint (`<issuer>/.well-known/jwks.json`). PyJWT's `PyJWKClient` caches the
keys and rotates automatically.

Key claims we care about (per Clerk docs):
    sub  — Clerk user ID (prefixed `user_...`)
    org_id / org_slug / org_role — organization context (if signed in as org)
    azp  — authorized party (frontend origin)
    iss  — must match CLERK_ISSUER
    exp  — expiry (Clerk defaults to 60 s — frontend auto-refreshes)

We raise a 401 on any verification failure. The FastAPI dependency wraps this
and also enforces that the token isn't issued from an unexpected origin.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ClerkClaims:
    sub: str
    org_id: str | None
    org_role: str | None
    org_slug: str | None
    email: str | None
    raw: dict[str, object]


class ClerkConfigError(RuntimeError):
    """Raised when Clerk verification is requested but not configured."""


class ClerkTokenError(RuntimeError):
    """Raised when a Clerk token is present but invalid."""


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    jwks_url = settings.CLERK_JWKS_URL
    if not jwks_url:
        if not settings.CLERK_ISSUER:
            raise ClerkConfigError(
                "Set CLERK_ISSUER (and optionally CLERK_JWKS_URL) to enable Clerk auth."
            )
        jwks_url = settings.CLERK_ISSUER.rstrip("/") + "/.well-known/jwks.json"
    # JWK rotation is infrequent; default 16-key cache is ample. PyJWT handles
    # its own TTL internally (hourly refresh) and re-fetches on kid miss.
    return PyJWKClient(jwks_url, cache_keys=True, max_cached_keys=16)


def verify_clerk_token(token: str) -> ClerkClaims:
    """Verify a Clerk JWT; raise ClerkTokenError on any failure."""
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token).key
        decode_kwargs: dict[str, object] = {
            "algorithms": ["RS256"],
            "options": {"require": ["exp", "sub", "iss"]},
        }
        if settings.CLERK_ISSUER:
            decode_kwargs["issuer"] = settings.CLERK_ISSUER
        if settings.CLERK_AUDIENCE:
            decode_kwargs["audience"] = settings.CLERK_AUDIENCE
        else:
            # Clerk tokens omit `aud` by default; skip that check.
            decode_kwargs["options"] = {
                **decode_kwargs["options"],  # type: ignore[dict-item]
                "verify_aud": False,
            }
        payload: dict[str, object] = jwt.decode(token, signing_key, **decode_kwargs)  # type: ignore[arg-type]
    except jwt.PyJWTError as exc:
        raise ClerkTokenError(f"Invalid Clerk token: {exc}") from exc

    return ClerkClaims(
        sub=str(payload["sub"]),
        org_id=payload.get("org_id"),  # type: ignore[arg-type]
        org_role=payload.get("org_role"),  # type: ignore[arg-type]
        org_slug=payload.get("org_slug"),  # type: ignore[arg-type]
        email=payload.get("email"),  # type: ignore[arg-type]
        raw=payload,
    )
