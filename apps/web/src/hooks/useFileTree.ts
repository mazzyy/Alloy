/**
 * useFileTree — fetches and manages the project's file tree state.
 *
 * Uses the `GET /api/v1/projects/{project_id}/files?path=...` endpoint.
 * Directories are lazily expanded on click — only the root is fetched
 * on mount, children are fetched when a directory is expanded.
 */

import { useState, useCallback, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "@/lib/api";

export interface FileEntry {
  path: string;
  is_dir: boolean;
  size_bytes: number | null;
}

interface FileListResponse {
  root: string;
  entries: FileEntry[];
  truncated: boolean;
}

interface UseFileTreeOptions {
  projectId: string | null;
  onSelectFile?: (path: string) => void;
}

export interface TreeNode {
  path: string;
  name: string;
  is_dir: boolean;
  size_bytes: number | null;
  /** Whether children have been loaded (null = not yet). */
  childrenLoaded: boolean;
  expanded: boolean;
  depth: number;
}

export function useFileTree({ projectId, onSelectFile }: UseFileTreeOptions) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set());
  const [dirEntries, setDirEntries] = useState<Map<string, FileEntry[]>>(new Map());

  // Fetch root entries.
  const rootQuery = useQuery({
    queryKey: ["files", projectId, "."],
    queryFn: async () => {
      const data = await api.request<FileListResponse>(
        `/projects/${projectId}/files?path=.`,
      );
      setDirEntries((m) => new Map(m).set(".", data.entries));
      return data;
    },
    enabled: !!projectId,
    retry: false,
    staleTime: 10_000,
  });

  const fetchDir = useCallback(
    async (dirPath: string) => {
      if (!projectId) return;
      const data = await api.request<FileListResponse>(
        `/projects/${projectId}/files?path=${encodeURIComponent(dirPath)}`,
      );
      setDirEntries((m) => new Map(m).set(dirPath, data.entries));
      return data;
    },
    [api, projectId],
  );

  const toggleDir = useCallback(
    async (dirPath: string) => {
      setExpandedDirs((prev) => {
        const next = new Set(prev);
        if (next.has(dirPath)) {
          next.delete(dirPath);
        } else {
          next.add(dirPath);
          // Fetch if we haven't loaded this dir's children yet.
          if (!dirEntries.has(dirPath)) {
            fetchDir(dirPath);
          }
        }
        return next;
      });
    },
    [dirEntries, fetchDir],
  );

  const selectFile = useCallback(
    (path: string) => {
      onSelectFile?.(path);
    },
    [onSelectFile],
  );

  // Build the flat tree for rendering.
  const flatTree = useMemo(() => {
    const result: TreeNode[] = [];

    function walk(parentPath: string, depth: number) {
      const entries = dirEntries.get(parentPath);
      if (!entries) return;

      // Sort: directories first, then alphabetical.
      const sorted = [...entries].sort((a, b) => {
        if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
        return a.path.localeCompare(b.path);
      });

      for (const entry of sorted) {
        const name = entry.path.split("/").pop() ?? entry.path;
        const expanded = expandedDirs.has(entry.path);
        result.push({
          path: entry.path,
          name,
          is_dir: entry.is_dir,
          size_bytes: entry.size_bytes,
          childrenLoaded: entry.is_dir ? dirEntries.has(entry.path) : false,
          expanded,
          depth,
        });
        if (entry.is_dir && expanded) {
          walk(entry.path, depth + 1);
        }
      }
    }

    walk(".", 0);
    return result;
  }, [dirEntries, expandedDirs]);

  const refreshTree = useCallback(() => {
    setDirEntries(new Map());
    setExpandedDirs(new Set());
    queryClient.invalidateQueries({ queryKey: ["files", projectId] });
  }, [projectId, queryClient]);

  return {
    flatTree,
    toggleDir,
    selectFile,
    refreshTree,
    isLoading: rootQuery.isLoading,
    isError: rootQuery.isError,
    error: rootQuery.error,
    /** True when the project has no workspace yet (409 from API). */
    noWorkspace:
      rootQuery.error instanceof Error &&
      rootQuery.error.message.includes("409"),
  };
}
