# Alloy

The wedge: every dominant AI code-generator today (Bolt, v0, Lovable, Cursor Composer, Replit Agent) is Node/Next/Supabase-centric because in-browser WASM runtimes can't run FastAPI. A Python-first generator with per-project cloud sandboxes and a fine-tuned apply model is a genuine market gap.

## Stack at a glance

| Layer            | Pick |
| ---------------- | ---- |
| Planner LLM      | Azure OpenAI `gpt-5-mini` (two regions, LiteLLM router, OpenAI + Claude fallbacks) |
| Apply LLM        | Morph `v3-fast` primary, Relace Apply 3 fallback (self-host later) |
| Orchestration    | Pydantic AI agents + LangGraph outer loop |
| Gateway          | FastAPI (async, stateless), Arq workers on Redis |
| Data plane       | Postgres (Neon in prod, local Postgres 16 in dev), Row-Level Security |
| Auth             | Clerk (Pro tier), WorkOS for enterprise SSO |
| Sandbox          | Daytona Cloud (primary), Fly Sprites (scale-out), Sandpack (lite preview) |
| Deploy targets   | GitHub App → Vercel (frontend) → Railway / Azure Container Apps (backend) |
| Observability    | Langfuse (self-host), Sentry, PostHog, Axiom |
| Object storage   | Cloudflare R2 |

See `roadmap.txt` for the full 18-week plan.

## Repo layout

```
.
├── apps/
│   ├── api/        # FastAPI gateway + Arq workers (Python, uv-managed)
│   └── web/        # React IDE shell (Vite + TS + Tailwind + shadcn + TanStack)
├── packages/
│   └── shared/     # AppSpec / BuildPlan schemas mirrored Python ↔ TS
├── .github/workflows/   # CI (ruff/mypy/pytest + tsc/eslint/vitest + Playwright smoke)
├── compose.yml          # Dev stack: api + web + postgres + redis
├── pnpm-workspace.yaml  # JS workspace config
├── .env.example         # Every service key the roadmap calls for
└── README.md
```

## Phase 0 quickstart

### Prerequisites

You need these on your PATH. Versions tested against:

| Tool             | Min version | Install |
| ---------------- | ----------- | ------- |
| Docker Desktop   | 25+         | https://docs.docker.com/desktop |
| `uv`             | 0.5+        | `brew install uv` |
| `pnpm`           | 9+          | `brew install pnpm` (or `corepack enable && corepack prepare pnpm@latest --activate`) |
| Node.js          | 22+ LTS     | `brew install node@22` |
| Python           | 3.12+       | `uv python install 3.12` (uv manages this automatically) |

Sanity check:

```bash
docker --version && uv --version && pnpm --version && node --version
```

### 1. Clone and seed env

```bash
git clone <this-repo> alloy && cd alloy
cp .env.example .env
```

Open `.env` and fill in at minimum:

```bash
AZURE_OPENAI_ENDPOINT="https://<your-resource>.openai.azure.com/"
AZURE_OPENAI_API_KEY="<your-key>"
AZURE_OPENAI_DEPLOYMENT="gpt-5-mini"     # deployment name in your Azure resource
AZURE_OPENAI_API_VERSION="2025-04-01-preview"

POSTGRES_PASSWORD="alloy-dev"             # anything — just keep api + db in sync
```

Everything else (`CLERK_*`, `GITHUB_APP_*`, `STRIPE_*`, etc.) can stay blank during Phase 0 — the backend detects `CLERK_ISSUER` is unset and enables a **dev-identity bootstrap** that returns `{"user_id": "dev_user", "tenant_id": "dev_tenant"}` without a real token. That's how `/api/v1/ping` works out of the box.

### 2. Install dependencies

```bash
pnpm install                                     # web workspace + shared TS
(cd apps/api && uv sync)                         # FastAPI + Arq + LiteLLM + Alembic
```

`uv sync` creates `apps/api/.venv` and resolves the full lockfile in ~10s.

### 3. Boot the stack

Two paths — pick one. Docker is the default; local is faster to iterate.

**Option A — everything in Docker Compose (recommended for first run):**

```bash
docker compose up -d db redis                    # Postgres 16 + Redis 7
(cd apps/api && uv run alembic upgrade head)     # run migrations once
docker compose up api web                        # start FastAPI + Vite
```

Leave the `api web` terminal running — both have hot reload.

**Option B — services in Docker, apps on host (snappier DX):**

```bash
docker compose up -d db redis
(cd apps/api && uv run alembic upgrade head)

# Terminal 1 — FastAPI with --reload
cd apps/api && uv run uvicorn app.main:app --reload --port 8000

# Terminal 2 — Vite
pnpm --filter web dev
```

### 4. Verify it's alive

```bash
# Liveness — always 200
curl -s http://localhost:8000/api/v1/health

# Readiness — checks Postgres + Redis
curl -s http://localhost:8000/api/v1/ready

# Clerk-protected ping — dev bootstrap returns dev_user/dev_tenant
curl -s http://localhost:8000/api/v1/ping

# Azure OpenAI streaming smoke test (expects AZURE_OPENAI_* filled in)
curl -N -X POST http://localhost:8000/api/v1/generate/echo \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Give me a five-word slogan.","reasoning_effort":"low"}'
```

Then open:

- <http://localhost:5173> — React IDE shell (Dashboard with ping card + echo card)
- <http://localhost:8000/docs> — FastAPI OpenAPI explorer

### 5. Run the tests

```bash
# Backend — ruff + mypy + pytest
cd apps/api
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -q
cd ../..

# Frontend — tsc + eslint + vitest
pnpm --filter web typecheck
pnpm --filter web lint
pnpm --filter web test -- --run
```

CI (`.github/workflows/ci.yml`) runs the same checks plus a Docker Compose smoke boot.

### Common gotchas

- **`/generate/echo` returns 500 "AZURE_OPENAI_ENDPOINT is required"** — fill in the Azure creds in `.env` and restart the API.
- **`ping` returns 401** — you set `CLERK_ISSUER` but didn't attach a real Clerk bearer. Either clear `CLERK_ISSUER` (dev bootstrap) or sign in through the React shell (which gets a real token from Clerk).
- **`alembic upgrade head` fails to connect** — make sure `docker compose up -d db` is up and `POSTGRES_PASSWORD` in `.env` matches what compose started Postgres with.
- **Vite fails with "Cannot find module @rollup/rollup-linux-*"** — you ran `pnpm install` on one OS and are trying to run Vite on another (e.g. via a Docker bind-mount from macOS into Linux). Run `pnpm install` again inside the target OS, or delete `node_modules` and reinstall.
- **`uv sync` pulls a new Python** — that's expected; uv manages its own interpreters under `~/.local/share/uv/python`.

## Phase 0 deliverable

A logged-in user (Clerk) hits `/api/v1/ping` and gets back `{ "ok": true, "tenant_id": ..., "user_id": ... }`. Azure OpenAI client is wired but only exposed as a `POST /api/v1/generate/echo` smoke endpoint for now — Phase 1 builds the real Spec Agent + Planner + Coder Agent on top.

## Roadmap

18-week, 2-senior-engineer plan documented in `roadmap.txt`. Phases:

0. Foundation (weeks 1–2) — **this milestone**
1. Core generation pipeline (weeks 3–6)
2. Surgical edits: Morph + visual picker + checkpoints (weeks 7–9)
3. GitHub + Docker + Vercel integrations (weeks 10–12)
4. Production hardening: billing, RLS, prompt-injection defenses (weeks 13–16)
5. Launch prep: final templates, SOC 2 gap, public beta (weeks 17–18)

## License

TBD — private during build.
