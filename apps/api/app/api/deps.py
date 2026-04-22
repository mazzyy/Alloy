"""FastAPI dependencies.

Tenant resolution rule (roadmap §8, multi-tenancy):
    Clerk JWT carries `org_id` as a custom claim. We extract it here and expose
    it as `Principal.tenant_id`. In Phase 4 a further dependency will call
    `SET LOCAL app.current_tenant = <tenant_id>` inside the per-request
    transaction so Postgres Row-Level Security enforces isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.core.clerk import ClerkConfigError, ClerkTokenError, verify_clerk_token
from app.core.config import settings


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    tenant_id: str  # org_id if present, otherwise user_id (personal workspace)
    org_role: str | None
    email: str | None


def _auth_disabled() -> bool:
    # Only allow unauthenticated access in local dev with no issuer configured.
    return settings.ENVIRONMENT == "local" and not settings.CLERK_ISSUER


async def get_current_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization is None or not authorization.lower().startswith("bearer "):
        if _auth_disabled():
            # Bootstrap identity so devs can hit /ping before wiring Clerk.
            return Principal(user_id="dev_user", tenant_id="dev_tenant", org_role=None, email=None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = verify_clerk_token(token)
    except ClerkConfigError as exc:
        # Misconfigured server — surface as 500 so it's loud in logs.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ClerkTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return Principal(
        user_id=claims.sub,
        tenant_id=claims.org_id or claims.sub,
        org_role=claims.org_role,
        email=claims.email,
    )


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
