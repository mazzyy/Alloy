# Alloy blocks

A **block** is a composable, versioned feature bundle the Alloy scaffolder
applies on top of the `base-react-fastapi` template. Blocks declare:

- **files**: files to create or overlay into the rendered project
- **deps**: Python / JS dependencies to add to `pyproject.toml` / `package.json`
- **env**: environment variables to append to `.env.example`
- **patches**: named AST patch points in the base template (Phase 2 — AST patches
  are anchor-comment based for now, full tree-sitter rewrite lands with the
  visual picker work)
- **imports / registrations**: `from X import Y` + router includes / feature flags

Every block ships a `block.yaml` manifest plus a `content/` tree with the files
it adds. Filenames under `content/` are relative to the generated project root.
Jinja templating is supported; the context is the same one the scaffolder passes
to the base template (`project`, `spec`, derived `answers`).

## Directory layout

```
blocks/
  auth/
    clerk/
      block.yaml
      content/
        backend/app/core/clerk.py
        frontend/src/auth/ClerkProvider.tsx
  storage/
    r2/
      block.yaml
      content/
        backend/app/core/r2.py
        backend/app/api/routes/uploads.py
```

## Anchor patches

Blocks can patch anchor comments in the base template:

```yaml
patches:
  - file: backend/app/api/main.py
    anchor: "# <<ALLOY_ROUTER_REGISTER>>"
    insert: "app.include_router(uploads.router)"
```

The scaffolder locates the anchor comment and inserts the block's content
immediately after it. This keeps blocks composable — multiple blocks can patch
the same file without stepping on each other. A missing anchor is a hard error
(fail fast in scaffold, not at runtime).

## Versioning

Every block has a `version: semver` field. Every generated project records the
full manifest (`base@1.4.2 + blocks: auth/clerk@0.3.1, storage/r2@0.2.0`) in
`.alloy/manifest.json`. Security CVEs trigger an agent-generated "template
update available" PR (Phase 4).
