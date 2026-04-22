#!/usr/bin/env bash
# Alloy — start the full dev stack.
#
# Usage:  ./dev.sh          (start everything)
#         ./dev.sh stop     (tear down)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"

stop() {
  echo "⏹  Stopping…"
  kill "$(cat "$ROOT/.api.pid" 2>/dev/null)" 2>/dev/null || true
  kill "$(cat "$ROOT/.web.pid" 2>/dev/null)" 2>/dev/null || true
  rm -f "$ROOT/.api.pid" "$ROOT/.web.pid"
  docker compose -f "$ROOT/compose.yml" down
  echo "✅  Stopped."
}

if [[ "${1:-}" == "stop" ]]; then stop; exit 0; fi

# Stop local Postgres if it's hogging port 5432
brew services stop postgresql@14 2>/dev/null || true

echo "🐘  Starting Postgres + Redis…"
docker compose -f "$ROOT/compose.yml" up -d db redis
sleep 3

echo "📦  Installing deps…"
(cd "$ROOT/apps/api" && uv sync --quiet)
(cd "$ROOT" && pnpm install --silent)

echo "🗄️  Running migrations…"
(cd "$ROOT/apps/api" && uv run alembic upgrade head)

echo "🚀  Starting API on :8000…"
(cd "$ROOT/apps/api" && uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
  echo $! > "$ROOT/.api.pid")

echo "🌐  Starting frontend on :5173…"
(cd "$ROOT" && pnpm web:dev &
  echo $! > "$ROOT/.web.pid")

echo ""
echo "═══════════════════════════════════════"
echo "  Frontend  → http://localhost:5173"
echo "  API docs  → http://localhost:8000/api/v1/docs"
echo "  Health    → http://localhost:8000/api/v1/health"
echo "═══════════════════════════════════════"
echo ""
echo "Run './dev.sh stop' to tear down."
wait
