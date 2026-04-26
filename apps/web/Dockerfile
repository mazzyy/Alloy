# syntax=docker/dockerfile:1.7
# Alloy web — multi-stage build per roadmap §7 guidance.
#   - Vite build with Node 22-alpine (NPM side only — no native Python wheels)
#   - Served via nginxinc/nginx-unprivileged (non-root, port 8080)
#   - Long-cache immutable assets, no-cache on index.html, /healthz endpoint
#
# IMPORTANT: this Dockerfile expects the build context to be the MONOREPO ROOT,
# not `apps/web`. The web package depends on `@alloy/shared` (workspace:*),
# which lives at `packages/shared/ts`, so the build context has to include
# that sibling directory plus the root pnpm-workspace.yaml + lockfile.
# compose.yml sets `context: .` accordingly.

FROM node:22-alpine AS builder
WORKDIR /repo

# pnpm via corepack.
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

# 1. Lockfile + workspace manifests first → maximize layer cache hits.
COPY pnpm-lock.yaml pnpm-workspace.yaml package.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/shared/ts/package.json packages/shared/ts/package.json

# Install deps for the whole workspace (web + @alloy/shared). pnpm reads
# pnpm-workspace.yaml to wire the workspace symlinks.
RUN pnpm install --frozen-lockfile=false

# 2. Source for the workspace dep + the web app.
COPY packages/shared/ts packages/shared/ts
COPY apps/web apps/web

# 3. Build the web package only (filter avoids walking @alloy/shared, which
#    is a source-only package — see packages/shared/ts/package.json).
RUN pnpm --filter web build

# ─── Runtime ──────────────────────────────────────────────────────────
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY --from=builder /repo/apps/web/dist /usr/share/nginx/html
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget --spider -q http://127.0.0.1:8080/healthz || exit 1
