# Alloy

A production-grade AI full-stack generator that ships **React + FastAPI + Postgres** apps at Cursor-grade edit speed.

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

```bash
# 1. Install toolchain
brew install uv pnpm docker
docker --version && uv --version && pnpm --version

# 2. Seed env
cp .env.example .env
# Fill in AZURE_OPENAI_* and (later) CLERK_*, POSTGRES_PASSWORD, etc.

# 3. Install dependencies
pnpm install                      # installs web workspace deps
cd apps/api && uv sync && cd ../..

# 4. Boot the stack
docker compose up -d db redis     # Postgres + Redis first
cd apps/api && uv run alembic upgrade head && cd ../..
docker compose up api web

# 5. Visit
#   http://localhost:5173   → React IDE shell
#   http://localhost:8000/docs → FastAPI OpenAPI explorer
#   http://localhost:8000/api/v1/health → unauthenticated health probe
#   http://localhost:8000/api/v1/ping   → Clerk-protected ping
```

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
