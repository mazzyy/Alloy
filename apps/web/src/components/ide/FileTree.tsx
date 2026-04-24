/**
 * FileTree — renders the project's file tree in the IDE sidebar.
 *
 * Lazily expands directories on click, shows file icons based on
 * extension, and highlights the currently selected file.
 */

import {
  FolderOpen,
  FolderClosed,
  FileCode2,
  FileJson,
  FileText,
  File,
  FileType,
  Database,
  Settings,
  RefreshCw,
  AlertCircle,
  FolderTree,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { TreeNode } from "@/hooks/useFileTree";

/* ── Icon mapping ─────────────────────────────────────────────── */

function fileIcon(name: string, isDir: boolean, expanded: boolean) {
  if (isDir) {
    return expanded ? (
      <FolderOpen className="h-4 w-4 text-accent" />
    ) : (
      <FolderClosed className="h-4 w-4 text-accent/70" />
    );
  }
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  switch (ext) {
    case "py":
      return <FileCode2 className="h-4 w-4 text-[#3572A5]" />;
    case "ts":
    case "tsx":
      return <FileCode2 className="h-4 w-4 text-[#3178C6]" />;
    case "js":
    case "jsx":
      return <FileCode2 className="h-4 w-4 text-[#F7DF1E]" />;
    case "json":
      return <FileJson className="h-4 w-4 text-warning" />;
    case "md":
    case "txt":
      return <FileText className="h-4 w-4 text-muted-foreground" />;
    case "css":
    case "scss":
      return <FileType className="h-4 w-4 text-[#563D7C]" />;
    case "sql":
      return <Database className="h-4 w-4 text-info" />;
    case "toml":
    case "yml":
    case "yaml":
    case "ini":
    case "cfg":
      return <Settings className="h-4 w-4 text-muted-foreground" />;
    default:
      return <File className="h-4 w-4 text-muted-foreground" />;
  }
}

/* ── Component ────────────────────────────────────────────────── */

interface FileTreeProps {
  flatTree: TreeNode[];
  activePath: string | null;
  onSelectFile: (path: string) => void;
  onToggleDir: (path: string) => void;
  onRefresh: () => void;
  isLoading: boolean;
  noWorkspace: boolean;
  className?: string;
}

export function FileTree({
  flatTree,
  activePath,
  onSelectFile,
  onToggleDir,
  onRefresh,
  isLoading,
  noWorkspace,
  className,
}: FileTreeProps) {
  return (
    <div className={cn("flex h-full flex-col", className)}>
      {/* Header */}
      <div className="flex h-[var(--tab-height)] items-center justify-between border-b border-border px-3">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Explorer
        </span>
        <button
          type="button"
          onClick={onRefresh}
          className="flex h-5 w-5 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground"
          title="Refresh file tree"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Tree */}
      <ScrollArea className="flex-1">
        {isLoading && (
          <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground">
            <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            Loading files…
          </div>
        )}

        {noWorkspace && (
          <div className="flex flex-col items-center gap-2 p-6 text-center text-xs text-muted-foreground">
            <FolderTree className="h-8 w-8 opacity-40" />
            <p>No workspace yet.</p>
            <p className="text-[10px]">
              Start a build to scaffold the project.
            </p>
          </div>
        )}

        {!isLoading && !noWorkspace && flatTree.length === 0 && (
          <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground">
            <AlertCircle className="h-3.5 w-3.5" />
            Empty workspace.
          </div>
        )}

        <div className="py-1">
          {flatTree.map((node) => (
            <button
              key={node.path}
              type="button"
              onClick={() =>
                node.is_dir ? onToggleDir(node.path) : onSelectFile(node.path)
              }
              className={cn(
                "flex w-full items-center gap-1.5 px-2 py-[3px] text-left text-[12px] transition-colors",
                "hover:bg-muted/50",
                activePath === node.path && "bg-accent/10 text-accent",
              )}
              style={{ paddingLeft: `${8 + node.depth * 14}px` }}
            >
              {/* Expand chevron for directories */}
              {node.is_dir && (
                <span
                  className={cn(
                    "flex h-3 w-3 items-center justify-center text-[8px] text-muted-foreground transition-transform",
                    node.expanded && "rotate-90",
                  )}
                >
                  ▶
                </span>
              )}
              {!node.is_dir && <span className="h-3 w-3" />}
              {fileIcon(node.name, node.is_dir, node.expanded)}
              <span className="truncate">{node.name}</span>
            </button>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
