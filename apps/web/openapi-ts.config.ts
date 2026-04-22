import { defineConfig } from "@hey-api/openapi-ts";

// Run with `pnpm --filter web generate-client`.
// The backend must be reachable — start with `uv run uvicorn app.main:app --reload`.
export default defineConfig({
  input: "http://localhost:8000/api/v1/openapi.json",
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
