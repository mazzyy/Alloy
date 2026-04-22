"""Unauthenticated health and readiness probes.

`/health` — process is alive (liveness). Always returns 200.
`/ready`  — process can serve traffic (readiness). Verifies DB + Redis.
"""

from __future__ import annotations

from typing import Literal

import redis.asyncio as redis
from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["ok", "down"]
    redis: Literal["ok", "down"]


@router.get("/health", response_model=HealthResponse, name="health")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version="0.0.0", environment=settings.ENVIRONMENT)


@router.get("/ready", response_model=ReadyResponse, name="ready", status_code=status.HTTP_200_OK)
async def ready() -> ReadyResponse:
    db_state: Literal["ok", "down"] = "ok"
    redis_state: Literal["ok", "down"] = "ok"

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_state = "down"

    r = redis.from_url(settings.REDIS_URL)
    try:
        await r.ping()
    except Exception:
        redis_state = "down"
    finally:
        await r.aclose()

    overall: Literal["ready", "degraded"] = (
        "ready" if db_state == "ok" and redis_state == "ok" else "degraded"
    )
    return ReadyResponse(status=overall, database=db_state, redis=redis_state)
