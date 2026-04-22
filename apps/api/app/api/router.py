"""Top-level API router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import generate, health, ping, plan, spec

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(ping.router)
api_router.include_router(generate.router)
api_router.include_router(spec.router)
api_router.include_router(plan.router)
