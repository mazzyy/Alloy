/**
 * Thin `fetch` wrapper that attaches the Clerk bearer token (if any).
 * In Phase 1 we swap this for the generated `@hey-api/client-fetch` client
 * plus a custom auth middleware.
 */

import { useIdentity } from "@/auth/AuthProvider";

const API_BASE = "/api/v1"; // Vite dev-proxies to the FastAPI gateway

export function useApi() {
  const { getToken } = useIdentity();

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = await getToken();
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
    if (!res.ok) {
      const text = await res.text();
      throw new ApiError(res.status, text || res.statusText);
    }
    return (await res.json()) as T;
  }

  async function stream(
    path: string,
    init: RequestInit,
    onChunk: (chunk: string) => void,
    onError?: (err: string) => void,
  ): Promise<void> {
    const token = await getToken();
    const headers = new Headers(init.headers);
    headers.set("Accept", "text/event-stream");
    headers.set("Content-Type", "application/json");
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
    if (!res.ok || !res.body) {
      const text = await res.text();
      throw new ApiError(res.status, text || res.statusText);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // Split on SSE event boundary.
      const events = buf.split("\n\n");
      buf = events.pop() ?? "";
      for (const evt of events) {
        const lines = evt.split("\n");
        let eventType = "message";
        let data = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventType = line.slice(7).trim();
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (!data) continue;
        if (data === "[DONE]") return;
        if (eventType === "error") {
          onError?.(data);
          return;
        }
        // Our backend escapes newlines — unescape for rendering.
        onChunk(data.replace(/\\n/g, "\n"));
      }
    }
  }

  return { request, stream };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
  ) {
    super(`[${status}] ${body}`);
  }
}
