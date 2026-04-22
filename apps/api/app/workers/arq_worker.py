"""Arq worker entrypoint.

Start with: `uv run arq app.workers.arq_worker.WorkerSettings`

Phase 1 will register the real generation tasks (`run_spec_agent`,
`run_coder_agent`, `run_validators`). For Phase 0 we only register a
placeholder so the worker boots cleanly and `/ready` can verify Redis.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import configure_logging

configure_logging()
log = structlog.get_logger(__name__)


async def ping_task(ctx: dict[str, Any], payload: str) -> str:
    log.info("alloy.worker.ping", payload=payload, job_id=ctx.get("job_id"))
    return f"pong: {payload}"


class WorkerSettings:
    functions = [ping_task]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = 10
    job_timeout = 600  # 10 min — generation jobs are long-running
    keep_result = 3600
    health_check_interval = 30

    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        log.info("alloy.worker.startup", redis=settings.REDIS_URL)

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        log.info("alloy.worker.shutdown")
