# Alloy web

React 19 + TypeScript + Vite + Tailwind v4 + shadcn-style components +
TanStack Query + Clerk.

## Dev

```bash
pnpm install
pnpm --filter web dev   # http://localhost:5173

# Generate the TS client from the running backend's OpenAPI spec.
pnpm --filter web generate-client
```

## Phase 0 scope

Three pages:

- `/sign-in` — Clerk-hosted (or bypassed in dev bootstrap).
- `/` — Dashboard with a Clerk-protected `/api/v1/ping` card and an
  Azure OpenAI `/api/v1/generate/echo` SSE streaming card.

Phase 1 replaces the dashboard with the real IDE shell (Monaco, file tree,
diff view, preview iframe, chat).
