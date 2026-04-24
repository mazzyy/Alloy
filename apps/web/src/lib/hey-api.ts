/**
 * Runtime configuration for the generated `@hey-api/client-fetch` client.
 *
 * The generator emits a `client.setConfig({...})` block that imports this
 * file — any defaults we set here apply to every generated SDK call.
 *
 * We don't attach the Clerk bearer token here because token retrieval is
 * hook-scoped (`useAuth().getToken()`). Instead, the IDE uses a tiny
 * `useClient` wrapper (see `src/lib/client.ts`) that re-configures the
 * client with the current token on every render of an auth-required
 * subtree.
 */
import type { CreateClientConfig } from "@hey-api/client-fetch";

export const createClientConfig: CreateClientConfig = (config) => ({
  ...config,
  // All routes are mounted under /api/v1 on the FastAPI gateway; in dev
  // Vite proxies this path to localhost:8000 (see vite.config.ts).
  baseUrl: "/api/v1",
});
