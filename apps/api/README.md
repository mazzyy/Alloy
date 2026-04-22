# Alloy API

FastAPI gateway + Arq workers for Alloy.

## Layout

```
app/
├── main.py                 # FastAPI app factory
├── api/
│   ├── router.py           # top-level router (include_router for each module)
│   ├── deps.py             # Clerk JWT verification → Principal
│   └── routes/
│       ├── health.py       # /health, /ready (unauthenticated)
│       ├── ping.py         # /ping (Clerk-protected, Phase 0 deliverable)
│       └── generate.py     # /generate/echo (Azure OpenAI SSE smoke)
├── core/
│   ├── config.py           # pydantic-settings — every env var
│   ├── logging.py          # structlog JSON in prod, pretty in local
│   ├── db.py               # async SQLAlchemy engine + session
│   ├── clerk.py            # JWKS-backed Clerk JWT verifier
│   └── llm.py              # LiteLLM router + raw Azure OpenAI client
└── workers/
    └── arq_worker.py       # Arq WorkerSettings (Phase 1: real generation tasks)
alembic/                    # migrations (empty until Phase 1)
tests/                      # pytest smoke tests
```

## Dev quickstart

```bash
cd apps/api
uv sync                        # install runtime + dev deps
uv run uvicorn app.main:app --reload --port 8000

# In another shell — Arq worker
uv run arq app.workers.arq_worker.WorkerSettings

# Tests
uv run pytest
uv run ruff check
uv run mypy app
```

## Auth behavior

| ENVIRONMENT | CLERK_ISSUER set? | No `Authorization` header | Bad bearer token | Valid Clerk token |
|-------------|-------------------|---------------------------|------------------|-------------------|
| `local`     | no                | ✅ dev_user / dev_tenant  | 401              | N/A               |
| `local`     | yes               | 401                       | 401              | ✅ principal       |
| `staging`/`production` | any     | 401                       | 401              | ✅ principal       |

The local bypass lets Phase 0 work without provisioning Clerk. Set
`CLERK_ISSUER=https://your-instance.clerk.accounts.dev` to enable real auth.

## Azure OpenAI smoke test

```bash
curl -N -X POST http://localhost:8000/api/v1/generate/echo \
  -H "Content-Type: application/json" \
  -d '{"prompt": "say hi in five words", "reasoning_effort": "low"}'
```

You should see an SSE stream of tokens followed by `data: [DONE]`.
