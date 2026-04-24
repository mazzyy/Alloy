import { defineConfig } from "@hey-api/openapi-ts";

// Generate the TypeScript client from a snapshot of the OpenAPI schema
// rather than hitting a live server. That makes client generation
// deterministic, offline-safe, and CI-friendly.
//
// Workflow:
//   1. Update FastAPI routes in apps/api.
//   2. From apps/api:  uv run python -m scripts.export_openapi
//      → writes apps/web/openapi.json (committed to the repo).
//   3. From apps/web:  pnpm generate-client
//      → regenerates src/client from the updated schema.
//
// CI runs `export_openapi --check` to fail builds whose committed
// schema is out of sync with the code.
export default defineConfig({
  input: "./openapi.json",
  output: {
    path: "src/client",
    format: "prettier",
    lint: "eslint",
  },
  plugins: [
    { name: "@hey-api/client-fetch", runtimeConfigPath: "./src/lib/hey-api.ts" },
    { name: "@hey-api/typescript", enums: "typescript" },
    "@hey-api/sdk",
  ],
});
