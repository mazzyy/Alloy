# syntax=docker/dockerfile:1.7
# Alloy API — multi-stage build per roadmap §7 guidance:
#   - uv for dep install with --mount=type=cache for layer caching
#   - slim-bookworm runtime (NEVER alpine — musl libc breaks asyncpg, pydantic-core wheels)
#   - non-root user (uid 10001)
#   - Gunicorn + UvicornWorker × 4 in prod

# ─── Builder ──────────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install deps first for better layer caching.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

# Copy source and install the project itself.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# ─── Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 alloy \
 && useradd  --system --uid 10001 --gid alloy --home-dir /app --shell /sbin/nologin alloy

WORKDIR /app
COPY --from=builder --chown=alloy:alloy /app /app

USER alloy

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health').read()" || exit 1

# Default command: production Gunicorn. docker-compose overrides this with
# `uvicorn --reload` for hot-reload during dev.
CMD ["gunicorn", "app.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", \
     "-b", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
