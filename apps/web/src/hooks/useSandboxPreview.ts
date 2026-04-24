/**
 * useSandboxPreview — polls the sandbox info endpoint for the preview URL.
 *
 * This hook provides the Daytona full-preview toggle for the IDE. When
 * a project has a running sandbox, it returns the live preview URL that
 * gets passed to PreviewPanel for iframe rendering.
 *
 * Polling strategy:
 *   - Starts polling when a projectId is provided
 *   - Fast poll (3s) when the sandbox is booting
 *   - Slow poll (10s) when running or no sandbox exists
 *   - Stops when the component unmounts
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { useApi } from "@/lib/api";

export type SandboxStatus =
  | "none"
  | "created"
  | "booting"
  | "running"
  | "archived"
  | "failed"
  | "destroyed"
  | "error"
  | "unknown";

export interface SandboxState {
  /** Current lifecycle status of the sandbox. */
  status: SandboxStatus;
  /** Live preview URL — only set when sandbox is running. */
  previewUrl: string | null;
  /** Backend port on the host. */
  backendPort: number | null;
  /** Frontend port on the host. */
  frontendPort: number | null;
  /** Sandbox identifier (e.g. "sbx-7f3a9b2c"). */
  sandboxId: string | null;
  /** Whether we're currently polling. */
  isPolling: boolean;
  /** Last polling error, if any. */
  error: string | null;
  /** Force an immediate refresh. */
  refresh: () => void;
}

interface SandboxInfoResponse {
  status: string;
  preview_url: string | null;
  backend_port: number | null;
  frontend_port: number | null;
  sandbox_id: string | null;
  workspace_path: string | null;
}

/* ── Poll intervals ──────────────────────────────────────────── */

const FAST_POLL_MS = 3_000; // booting → check frequently
const SLOW_POLL_MS = 10_000; // running / idle → check less

function pollInterval(status: SandboxStatus): number {
  switch (status) {
    case "booting":
    case "created":
      return FAST_POLL_MS;
    default:
      return SLOW_POLL_MS;
  }
}

/* ── Hook ────────────────────────────────────────────────────── */

export function useSandboxPreview(
  projectId: string | null | undefined,
): SandboxState {
  const api = useApi();
  const [status, setStatus] = useState<SandboxStatus>("none");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [backendPort, setBackendPort] = useState<number | null>(null);
  const [frontendPort, setFrontendPort] = useState<number | null>(null);
  const [sandboxId, setSandboxId] = useState<string | null>(null);
  const [isPolling, setIsPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const fetchStatus = useCallback(async () => {
    if (!projectId) return;

    try {
      const data = await api.request<SandboxInfoResponse>(
        `/projects/${projectId}/sandbox`,
      );

      if (!mountedRef.current) return;

      const s = data.status as SandboxStatus;
      setStatus(s);
      setPreviewUrl(data.preview_url);
      setBackendPort(data.backend_port);
      setFrontendPort(data.frontend_port);
      setSandboxId(data.sandbox_id);
      setError(null);

      // Schedule next poll.
      timerRef.current = setTimeout(() => {
        if (mountedRef.current) fetchStatus();
      }, pollInterval(s));
    } catch (e) {
      if (!mountedRef.current) return;
      setError(e instanceof Error ? e.message : String(e));

      // Retry on error with slow interval.
      timerRef.current = setTimeout(() => {
        if (mountedRef.current) fetchStatus();
      }, SLOW_POLL_MS);
    }
  }, [api, projectId]);

  // Start/stop polling when projectId changes.
  useEffect(() => {
    mountedRef.current = true;

    if (!projectId) {
      setStatus("none");
      setPreviewUrl(null);
      setBackendPort(null);
      setFrontendPort(null);
      setSandboxId(null);
      setIsPolling(false);
      return;
    }

    setIsPolling(true);
    fetchStatus();

    return () => {
      mountedRef.current = false;
      setIsPolling(false);
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [projectId, fetchStatus]);

  const refresh = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    fetchStatus();
  }, [fetchStatus]);

  return {
    status,
    previewUrl,
    backendPort,
    frontendPort,
    sandboxId,
    isPolling,
    error,
    refresh,
  };
}
