"""`compose.alloy.yml` — the sandbox-specific Docker Compose override.

The base template ships `compose.yml` + `compose.override.yml` configured
for Traefik-routed local dev on fixed host ports (5432, 8000, 5173, 8080,
1080…). Those are fine for the *template* as a single-machine dev loop,
but they fall apart the moment we want two sandboxes running concurrently.

For per-project sandboxes we need:

* project-unique published ports (so N sandboxes coexist)
* **no host port for Postgres** — the db sits on the sandbox's internal
  network; the backend reaches it at `db:5432`. Users don't `psql` in;
  they point Alembic/SQL at the backend's migrations endpoint.
* no proxy/adminer/mailcatcher/playwright — sandbox preview is a narrow
  surface (frontend + backend + db). The template's extras are great in
  its own dev loop but waste RAM and ports here.
* a deterministic compose project name so `docker compose ... down` finds
  the right set on archive.

We drop a freshly rendered `compose.alloy.yml` into the workspace and
invoke `docker compose -f compose.alloy.yml -p <compose_project> …`. The
template's own compose files are ignored — their Dockerfiles still live
at the expected paths (`backend/Dockerfile`, `frontend/Dockerfile`) so
build contexts stay the same.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComposeRenderParams:
    """Everything `render_alloy_compose` needs.

    Kept as a dataclass so the manager can unit-test compose generation
    without pulling in the port allocator or runtime.
    """

    compose_project: str  # e.g. "alloy-sbx-7f3a9b2c"
    backend_port: int  # host port published → container:8000
    frontend_port: int  # host port published → container:80
    postgres_user: str
    postgres_password: str
    postgres_db: str
    secret_key: str
    # Optional extra env for the backend service (forwarded verbatim).
    # Useful for block-provided vars (CLERK_*, R2_*, etc.) that the
    # scaffolder wrote into `.env.example` — the sandbox manager reads
    # `.env` and merges.
    backend_env: dict[str, str] | None = None


def _quote(v: str) -> str:
    """Quote a YAML scalar value — good-enough single-quote escaping.

    Compose tolerates unquoted values for most strings but generated
    secrets can contain `:`, `#`, or start with `!`, which YAML parses
    specially. Single-quoting with `''` escape is robust for anything
    short of embedded newlines (secrets.token_urlsafe doesn't emit any).
    """
    return "'" + v.replace("'", "''") + "'"


def render_alloy_compose(params: ComposeRenderParams) -> str:
    """Render the sandbox `compose.alloy.yml` as a YAML string.

    Hand-rolled string formatting rather than PyYAML because:

    * The output is small + stable; round-tripping through PyYAML loses
      our comments, which are load-bearing for humans debugging a stuck
      sandbox.
    * We control every value, so there's no injection surface from user
      input (ports are ints; names/secrets come from our own generators).

    The generated file is checked into the sandbox workspace but marked
    `.gitignore`-d — it changes every time ports change, so committing it
    would create noise diffs. See `LocalSandboxManager._write_gitignore`.
    """
    env = params.backend_env or {}
    # Fixed-order so the rendered file is byte-stable for the same inputs.
    backend_env_lines: list[str] = []
    base_backend_env = {
        "POSTGRES_SERVER": "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": params.postgres_user,
        "POSTGRES_PASSWORD": params.postgres_password,
        "POSTGRES_DB": params.postgres_db,
        "SECRET_KEY": params.secret_key,
        # Let the backend know where it's published — generated TS
        # client uses this to point at the sandbox's own URL.
        "BACKEND_PUBLIC_URL": f"http://localhost:{params.backend_port}",
    }
    merged = {**base_backend_env, **env}
    for k in sorted(merged):
        backend_env_lines.append(f"      {k}: {_quote(merged[k])}")
    backend_env_block = "\n".join(backend_env_lines)

    return f"""# compose.alloy.yml — managed by the Alloy sandbox manager. DO NOT EDIT BY HAND.
# Regenerated on every `sandbox.create()` / port-rebalance.
name: {params.compose_project}

services:
  db:
    image: postgres:18-alpine
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${{POSTGRES_USER}} -d $${{POSTGRES_DB}}"]
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 15s
    environment:
      POSTGRES_USER: {_quote(params.postgres_user)}
      POSTGRES_PASSWORD: {_quote(params.postgres_password)}
      POSTGRES_DB: {_quote(params.postgres_db)}
    volumes:
      - db-data:/var/lib/postgresql/data

  backend:
    build:
      context: .
      dockerfile: backend/Dockerfile
    command: ["fastapi", "run", "--reload", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]
    environment:
{backend_env_block}
    ports:
      - "127.0.0.1:{params.backend_port}:8000"
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ./backend:/app
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/utils/health-check/', timeout=2) or sys.exit(0)"]
      interval: 10s
      timeout: 3s
      retries: 6
      start_period: 30s

  frontend:
    build:
      context: .
      dockerfile: frontend/Dockerfile
      args:
        VITE_API_URL: "http://localhost:{params.backend_port}"
        NODE_ENV: "development"
    ports:
      - "127.0.0.1:{params.frontend_port}:80"
    depends_on:
      - backend

volumes:
  db-data:
"""


def preview_url_for(frontend_port: int, host: str = "localhost") -> str:
    """Where the generated frontend is reachable after boot."""
    return f"http://{host}:{frontend_port}"
