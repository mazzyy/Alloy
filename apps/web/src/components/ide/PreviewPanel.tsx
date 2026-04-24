/**
 * PreviewPanel — iframe preview of the generated project.
 *
 * Phase 1: simple placeholder / iframe pointing to the sandbox URL.
 * Phase 2 will add Sandpack lite preview for instant React-only rendering.
 */

import { ExternalLink, RefreshCw, Globe, Eye } from "lucide-react";
import { cn } from "@/lib/cn";

interface PreviewPanelProps {
  previewUrl: string | null;
  className?: string;
}

export function PreviewPanel({ previewUrl, className }: PreviewPanelProps) {
  return (
    <div className={cn("flex h-full flex-col", className)}>
      {/* Toolbar */}
      <div className="flex h-[var(--tab-height)] items-center gap-2 border-b border-border px-3">
        <Eye className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Preview
        </span>
        {previewUrl && (
          <>
            <div className="flex-1">
              <div className="flex items-center gap-1.5 rounded-md border border-border bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">
                <Globe className="h-3 w-3" />
                <span className="truncate">{previewUrl}</span>
              </div>
            </div>
            <button
              type="button"
              onClick={() => {
                const iframe = document.querySelector<HTMLIFrameElement>(
                  "#preview-iframe",
                );
                if (iframe) iframe.src = previewUrl;
              }}
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

      {/* Preview area */}
      <div className="relative flex-1 bg-muted/20">
        {previewUrl ? (
          <iframe
            id="preview-iframe"
            src={previewUrl}
            className="h-full w-full border-0"
            title="Project preview"
            sandbox="allow-scripts allow-forms allow-same-origin allow-popups"
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
            <Globe className="h-10 w-10 opacity-20" />
            <p className="text-sm">No preview available</p>
            <p className="max-w-[200px] text-center text-[11px] opacity-60">
              Build the project to see a live preview here.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
