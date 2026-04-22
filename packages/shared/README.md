# @alloy/shared

Canonical schemas mirrored across Python (backend + generation agents) and
TypeScript (frontend + type-safe API client).

The key types defined here (as **drafts** — Phase 1 fleshes them out):

| Type         | Purpose |
| ------------ | ------- |
| `AppSpec`    | What the user wants to build. Entities, routes, pages, auth, integrations. The **Intake + Spec Agent** produces this; the user edits it in the UI before code generation starts. |
| `BuildPlan`  | An ordered DAG of file operations the **Planner Agent** emits from `AppSpec`. The LangGraph outer loop consumes it. |
| `ToolCall`   | Union of every tool the Coder Agent can invoke (`read_file`, `apply_patch`, `run_command`, …). |

Phase 0 ships only the outer shapes; Phase 1 fills in the fields.

## Layout

```
packages/shared/
├── python/           # Installed into apps/api via `uv add --editable ../../packages/shared/python`
│   └── alloy_shared/
│       ├── __init__.py
│       ├── spec.py
│       └── plan.py
└── ts/
    ├── package.json  # Linked via pnpm workspace; imported as `@alloy/shared`
    ├── tsconfig.json
    └── src/
        ├── index.ts
        ├── spec.ts
        └── plan.ts
```

## Why not pydantic-to-zod codegen?

We evaluated it; the output is brittle for discriminated unions which is
exactly where `AppSpec.auth` lives. Hand-mirrored is cheaper to maintain and
doesn't block on a new tool. CI adds a round-trip test in Phase 1
(`pytest -m shared-roundtrip`) to catch drift.
