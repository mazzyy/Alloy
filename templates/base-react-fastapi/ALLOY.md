# Base: React + FastAPI

Vendored from [`fastapi/full-stack-fastapi-template`](https://github.com/fastapi/full-stack-fastapi-template)
(MIT). The original upstream docs (`README.md`, `development.md`, `deployment.md`)
are preserved one directory up — read those for general-purpose usage.

This file captures how **Alloy** uses the template.

## How Alloy consumes this template

1. The **Planner Agent** emits a `BuildPlan` with `base_template: "react-fastapi"`
   and a `blocks: [...]` list (e.g. `auth/clerk`, `storage/r2`).
2. The **scaffolder** (Phase 1 wk3+) is a thin wrapper around `copier copy`
   that renders this directory into the per-project Daytona/local-Docker
   sandbox, then applies each block's AST patches on top.
3. The **Coder Agent** only edits files inside the rendered project's
   `user_domain/` surface — not these template files. A filesystem policy
   rejects writes to `base/` and `blocks/` paths from agent tools.

## What we changed vs upstream

| Area | Change | Why |
| ---- | ------ | --- |
| UI kit | Chakra → shadcn + Tailwind v4 (Phase 1) | Alloy's design system; shadcn matches Cursor/Lovable polish |
| Auth | Scaffolded JWT → pluggable via `auth/*` blocks | Alloy's first block is `auth/clerk` |
| Client codegen | openapi-ts → `@hey-api/openapi-ts` | New de-facto standard; typed TanStack Query hooks |
| Copier `_tasks` | extended to run `alembic upgrade head` post-render | Sandbox boots with migrated DB |

## Upgrade policy

Upstream ships on a rolling release. Alloy pins the vendored copy at a known-good
commit and cuts template releases by semver (see `templates/CHANGELOG.md`, added
when we ship the first paid user).

To refresh from upstream:

```bash
# From repo root
scripts/refresh-base-template.sh  # dry-run diff of upstream vs vendored
# review, merge, bump templates/base-react-fastapi/VERSION
```
