# syntax=docker/dockerfile:1.7
# Alloy API — multi-stage build per roadmap §7 guidance:
#   - uv for dep install with --mount=type=cache for layer caching
#   - slim-bookworm runtime (NEVER alpine — musl libc breaks asyncpg, pydantic-core wheels)
#   - non-root user (uid 10001)
#   - Gunicorn + UvicornWorker × 4 in prod
#
# IMPORTANT: this Dockerfile expects the build context to be the MONOREPO ROOT,
# not `apps/api`. The api package depends on `alloy-shared` via a relative
# uv source (`../../packages/shared/python`), so the build context has to
# include that sibling directory. compose.yml sets `context: .` accordingly.

# ─── Builder ──────────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Build at the SAME absolute path the runtime image will use (/srv) so the
# editable install of `alloy-shared` records `/srv/packages/shared/python`
# in the venv's RECORD/.pth files — which is exactly where the runtime
# stage copies it to. If we built at /app and copied to /srv, the venv's
# pip-recorded paths would point at /app/... and the import would fail.
#
#   /srv/apps/api               ← WORKDIR for build + runtime
#   /srv/packages/shared/python ← editable workspace dep

# Copy the workspace dep FIRST (rarely changes) for better layer caching.
COPY packages/shared/python /srv/packages/shared/python

WORKDIR /srv/apps/api

# Install deps first for better layer caching.
COPY apps/api/pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

# Copy source and install the project itself.
COPY apps/api/app ./app
COPY apps/api/alembic ./alembic
COPY apps/api/alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# ─── Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/srv/apps/api/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 alloy \
 && useradd  --system --uid 10001 --gid alloy --home-dir /srv --shell /sbin/nologin alloy

# Builder placed everything at /srv/{apps,packages} so paths recorded in
# the venv's editable installs stay valid here verbatim.
COPY --from=builder --chown=alloy:alloy /srv/apps/api /srv/apps/api
COPY --from=builder --chown=alloy:alloy /srv/packages /srv/packages

WORKDIR /srv/apps/api

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
