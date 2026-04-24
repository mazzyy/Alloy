# Alloy web

React 19 + TypeScript + Vite + Tailwind v4 + shadcn-style components +
TanStack Query + Clerk.

## Dev

```bash
pnpm install
pnpm --filter web dev   # http://localhost:5173
```

### Regenerating the typed API client

The client in `src/client/` is generated from `openapi.json`, which is a
committed snapshot of the FastAPI schema. This keeps regeneration
deterministic and offline-friendly.

Workflow:

```bash
# 1. Export the current schema from the backend code (no server needed).
cd ../api
uv run python -m scripts.export_openapi   # → apps/web/openapi.json

# 2. Regenerate the TypeScript SDK + types.
cd ../web
pnpm generate-client                      # → src/client/
```

CI runs `uv run python -m scripts.export_openapi --check` to fail the
build when the committed schema drifts from the FastAPI code.

## Phase 0 scope

Three pages:

- `/sign-in` — Clerk-hosted (or bypassed in dev bootstrap).
- `/` — Dashboard with a Clerk-protected `/api/v1/ping` card and an
  Azure OpenAI `/api/v1/generate/echo` SSE streaming card.

Phase 1 replaces the dashboard with the real IDE shell (Monaco, file tree,
diff view, preview iframe, chat).
