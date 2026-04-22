# syntax=docker/dockerfile:1.7
# Alloy web — multi-stage build per roadmap §7 guidance.
#   - Vite build with Node 22-alpine (NPM side only — no native Python wheels)
#   - Served via nginxinc/nginx-unprivileged (non-root, port 8080)
#   - Long-cache immutable assets, no-cache on index.html, /healthz endpoint

FROM node:22-alpine AS builder
WORKDIR /app

# pnpm via corepack.
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

COPY package.json pnpm-lock.yaml* ./
RUN pnpm fetch --prod || true

COPY . .
RUN pnpm install --frozen-lockfile=false && pnpm build

# ─── Runtime ──────────────────────────────────────────────────────────
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget --spider -q http://127.0.0.1:8080/healthz || exit 1
