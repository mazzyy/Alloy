"""Clerk-protected smoke endpoint — the Phase 0 deliverable."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentPrincipal

router = APIRouter(tags=["ping"])


class PingResponse(BaseModel):
    ok: bool
    user_id: str
    tenant_id: str
    org_role: str | None
    email: str | None


@router.get("/ping", response_model=PingResponse, name="ping")
async def ping(principal: CurrentPrincipal) -> PingResponse:
    return PingResponse(
        ok=True,
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        org_role=principal.org_role,
        email=principal.email,
    )
