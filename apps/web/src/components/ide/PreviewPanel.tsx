/**
 * PreviewPanel — dual-mode preview of the generated project.
 *
 * Two tiers per roadmap §6:
 *   1. **Lite preview** (Sandpack) — instant React-only rendering from
 *      the AppSpec, with mock data fixtures. Available as soon as the
 *      spec is proposed, no backend boot needed.
 *   2. **Full preview** (iframe) — points to the real Daytona sandbox
 *      URL once the project is built. Shows the actual FastAPI + React stack.
 *
 * The panel auto-selects Sandpack when no sandbox URL is available,
 * and users can toggle between the two modes via the toolbar.
 */

import { useMemo, useState, useCallback } from "react";
import {
  SandpackProvider,
  SandpackPreview,
  SandpackConsole,
} from "@codesandbox/sandpack-react";
import {
  ExternalLink,
  RefreshCw,
  Globe,
  Eye,
  Zap,
  Server,
  Terminal,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { generateSandpackFiles } from "@/lib/sandpack-gen";
import type { AppSpec } from "@alloy/shared";

/* ── Types ──────────────────────────────────────────────────────── */

type PreviewMode = "sandpack" | "iframe";

interface PreviewPanelProps {
  /** The generated AppSpec — drives the Sandpack lite preview. */
  spec: AppSpec | null;
  /** Full sandbox preview URL (available after build). */
  previewUrl: string | null;
  className?: string;
}

/* ── Component ──────────────────────────────────────────────────── */

export function PreviewPanel({
  spec,
  previewUrl,
  className,
}: PreviewPanelProps) {
  const hasSandpack = spec != null && spec.pages.length > 0;
  const hasIframe = previewUrl != null;

  const [mode, setMode] = useState<PreviewMode>(
    hasIframe ? "iframe" : "sandpack",
  );
  const [showConsole, setShowConsole] = useState(false);

  // Generate Sandpack files from the AppSpec.
  const sandpackFiles = useMemo(() => {
    if (!spec) return null;
    return generateSandpackFiles(spec);
  }, [spec]);

  const handleRefreshIframe = useCallback(() => {
    const iframe = document.querySelector<HTMLIFrameElement>("#preview-iframe");
    if (iframe && previewUrl) iframe.src = previewUrl;
  }, [previewUrl]);

  // Determine effective mode.
  const effectiveMode =
    mode === "iframe" && hasIframe
      ? "iframe"
      : hasSandpack
        ? "sandpack"
        : mode;

  return (
    <div className={cn("flex h-full flex-col", className)}>
      {/* ── Toolbar ─────────────────────────────────────────── */}
      <div className="flex h-[var(--tab-height)] items-center gap-2 border-b border-border px-3">
        <Eye className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Preview
        </span>

        {/* Mode toggle */}
        {(hasSandpack || hasIframe) && (
          <div className="ml-2 flex items-center rounded-md border border-border bg-muted/30 p-0.5">
            {hasSandpack && (
              <button
                type="button"
                onClick={() => setMode("sandpack")}
                className={cn(
                  "flex items-center gap-1 rounded-sm px-2 py-0.5 text-[10px] font-medium transition-colors",
                  effectiveMode === "sandpack"
                    ? "bg-accent/20 text-accent"
                    : "text-muted-foreground hover:text-foreground",
                )}
                title="Lite preview (React only, mock data)"
              >
                <Zap className="h-2.5 w-2.5" />
                Lite
              </button>
            )}
            {hasIframe && (
              <button
                type="button"
                onClick={() => setMode("iframe")}
                className={cn(
                  "flex items-center gap-1 rounded-sm px-2 py-0.5 text-[10px] font-medium transition-colors",
                  effectiveMode === "iframe"
                    ? "bg-success/20 text-success"
                    : "text-muted-foreground hover:text-foreground",
                )}
                title="Full preview (sandbox)"
              >
                <Server className="h-2.5 w-2.5" />
                Full
              </button>
            )}
          </div>
        )}

        <div className="flex-1" />

        {/* Console toggle (Sandpack mode only) */}
        {effectiveMode === "sandpack" && hasSandpack && (
          <button
            type="button"
            onClick={() => setShowConsole(!showConsole)}
            className={cn(
              "flex h-5 w-5 items-center justify-center rounded-sm transition-colors",
              showConsole
                ? "text-accent"
                : "text-muted-foreground hover:text-foreground",
            )}
            title="Toggle console"
          >
            <Terminal className="h-3 w-3" />
          </button>
        )}

        {/* Iframe controls */}
        {effectiveMode === "iframe" && previewUrl && (
          <>
            <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md border border-border bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">
              <Globe className="h-3 w-3 flex-shrink-0" />
              <span className="truncate">{previewUrl}</span>
            </div>
            <button
              type="button"
              onClick={handleRefreshIframe}
              className="flex h-5 w-5 items-center justify-center rounded-sm text-muted-foreground hover:text-foreground"
              title="Refresh preview"
            >
              <RefreshCw className="h-3 w-3" />
            </button>
            <a
              href={previewUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex h-5 w-5 items-center justify-center rounded-sm text-muted-foreground hover:text-foreground"
              title="Open in new tab"
            >
              <ExternalLink className="h-3 w-3" />
            </a>
          </>
        )}
      </div>

      {/* ── Preview content ────────────────────────────────── */}
      <div className="relative flex-1 overflow-hidden bg-muted/20">
        {effectiveMode === "sandpack" && sandpackFiles ? (
          <SandpackLitePreview
            files={sandpackFiles}
            showConsole={showConsole}
          />
        ) : effectiveMode === "iframe" && previewUrl ? (
          <iframe
            id="preview-iframe"
            src={previewUrl}
            className="h-full w-full border-0"
            title="Full project preview"
            sandbox="allow-scripts allow-forms allow-same-origin allow-popups"
          />
        ) : (
          <EmptyState hasSpec={spec != null} />
        )}
      </div>
    </div>
  );
}

/* ── Sandpack wrapper ──────────────────────────────────────────── */

function SandpackLitePreview({
  files,
  showConsole,
}: {
  files: Record<string, string>;
  showConsole: boolean;
}) {
  return (
    <SandpackProvider
      template="react"
      files={files}
      customSetup={{
        dependencies: {
          "react-router-dom": "^7.0.0",
        },
      }}
      theme="dark"
      options={{
        externalResources: [],
        classes: {
          "sp-wrapper": "sandpack-wrapper",
          "sp-layout": "sandpack-layout",
        },
      }}
    >
      <div className="flex h-full flex-col">
        <div className={cn("flex-1", showConsole && "h-[60%]")}>
          <SandpackPreview
            showOpenInCodeSandbox={false}
            showRefreshButton={true}
            style={{ height: "100%" }}
          />
        </div>
        {showConsole && (
          <div className="h-[40%] border-t border-border">
            <SandpackConsole style={{ height: "100%" }} />
          </div>
        )}
      </div>
    </SandpackProvider>
  );
}

/* ── Empty state ───────────────────────────────────────────────── */

function EmptyState({ hasSpec }: { hasSpec: boolean }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
      {hasSpec ? (
        <>
          <Loader2 className="h-8 w-8 animate-spin opacity-20" />
          <p className="text-sm">Generating preview…</p>
        </>
      ) : (
        <>
          <Globe className="h-10 w-10 opacity-20" />
          <p className="text-sm">No preview available</p>
          <p className="max-w-[220px] text-center text-[11px] opacity-60">
            Propose a spec to see an instant lite preview, or build the
            project for a full sandbox preview.
          </p>
        </>
      )}
    </div>
  );
}
