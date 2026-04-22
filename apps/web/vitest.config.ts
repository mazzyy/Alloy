import { defineConfig } from "vitest/config";
import path from "node:path";

/**
 * Minimal Vitest config — we rely on Vitest's built-in esbuild transform for
 * TSX rather than importing the @vitejs/plugin-react-swc plugin, which
 * triggers a vite@5 vs vite@6 type clash through vitest@2's transitive deps.
 */
export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  esbuild: {
    jsx: "automatic",
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
    },
  },
});
