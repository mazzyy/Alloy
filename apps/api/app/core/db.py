"""Async SQLAlchemy engine + session factory.

Phase 4 will attach the per-request `SET LOCAL app.current_tenant` hook that
Postgres Row-Level Security reads from (roadmap §8). For Phase 0 we only need
a working engine so that `alembic upgrade head` succeeds and health checks can
verify connectivity.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    str(settings.DATABASE_URL_ASYNC),
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
