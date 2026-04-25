"""FastAPI gateway entrypoint.

Responsibilities (per roadmap §2):
    - Clerk JWT verification + tenant resolution (handled in app.api.deps)
    - slowapi rate-limit (Redis, handled in app.core.rate_limit)
    - stream orchestration for generation jobs (Arq, handled in app.workers)
    - structured logging + Sentry

This module stays intentionally thin. Generation work runs in Arq workers so
gateway replicas remain stateless and horizontally scalable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.migrations import ensure_schema_for_environment

configure_logging()
log = structlog.get_logger(__name__)


def _unique_id(route: APIRoute) -> str:
    # Stable operation IDs so the @hey-api/openapi-ts client produces
    # nice TypeScript function names. Pattern: `<tag>-<name>`.
    if route.tags:
        return f"{route.tags[0]}-{route.name}"
    return route.name


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("alloy.api.startup", env=settings.ENVIRONMENT, version=app.version)
    if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
        sentry_sdk.init(
            dsn=str(settings.SENTRY_DSN),
            environment=settings.ENVIRONMENT,
            traces_sample_rate=0.1,
            profiles_sample_rate=0.1,
        )
    # Self-heal the gateway schema in local dev (auto `alembic upgrade
    # head`); in staging/prod we *check* and log loudly instead of
    # silently mutating the schema. Without this, adding a migration
    # like `0002_phase1_build_runs` lands a fresh dev DB in a state
    # where the first /build/run dies mid-stream with `relation
    # "build_runs" does not exist`.
    await ensure_schema_for_environment()
    yield
    log.info("alloy.api.shutdown")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.0.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=None,
    generate_unique_id_function=_unique_id,
    lifespan=lifespan,
)

# CORS — explicit origin list required with allow_credentials=True
# (Chrome + Firefox both reject "*" when credentials are sent).
if settings.all_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.all_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router, prefix=settings.API_V1_STR)
