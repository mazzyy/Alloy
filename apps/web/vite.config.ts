import path from "node:path";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react-swc";
import tailwindcss from "@tailwindcss/vite";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const apiTarget = env.VITE_API_URL ?? "http://localhost:8000";

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
        // Shared Pydantic↔TS schemas live in the monorepo, imported by path
        // alias rather than a separate pnpm workspace member so Phase 0's
        // single `pnpm install` keeps working.
        "@alloy/shared": path.resolve(__dirname, "../../packages/shared/ts/src/index.ts"),
      },
    },
    server: {
      port: 5173,
      strictPort: true,
      // Proxy /api → backend so the browser never hits CORS in dev.
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          // SSE / chunked streaming requires no buffering.
          ws: false,
          configure: (proxy) => {
            proxy.on("proxyReq", (proxyReq) => {
              proxyReq.setHeader("accept-encoding", "identity");
            });
          },
        },
      },
    },
    build: {
      sourcemap: true,
      target: "es2022",
    },
  };
});
