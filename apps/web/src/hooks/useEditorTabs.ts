/**
 * useEditorTabs — manages open file tabs and their content.
 *
 * Content is fetched from `GET /api/v1/projects/{project_id}/files/content?path=...`
 * and cached locally. Tabs persist for the session — no disk storage.
 */

import { useState, useCallback } from "react";
import { useApi } from "@/lib/api";

interface FileContentResponse {
  path: string;
  content: string;
  start_line: number;
  end_line: number;
  line_count: number;
  clipped: boolean;
}

export interface EditorTab {
  path: string;
  label: string;
  language: string;
  content: string;
  loading: boolean;
  error: string | null;
}

const EXT_LANG: Record<string, string> = {
  py: "python",
  ts: "typescript",
  tsx: "typescriptreact",
  js: "javascript",
  jsx: "javascriptreact",
  json: "json",
  md: "markdown",
  yml: "yaml",
  yaml: "yaml",
  toml: "toml",
  css: "css",
  html: "html",
  sql: "sql",
  sh: "shell",
  bash: "shell",
  dockerfile: "dockerfile",
  ini: "ini",
  cfg: "ini",
  env: "plaintext",
  txt: "plaintext",
  gitignore: "plaintext",
};

function detectLanguage(path: string): string {
  const name = path.split("/").pop()?.toLowerCase() ?? "";
  if (name === "dockerfile") return "dockerfile";
  if (name.startsWith(".env")) return "plaintext";
  const ext = name.split(".").pop() ?? "";
  return EXT_LANG[ext] ?? "plaintext";
}

export function useEditorTabs(projectId: string | null) {
  const api = useApi();
  const [tabs, setTabs] = useState<EditorTab[]>([]);
  const [activeIdx, setActiveIdx] = useState<number>(-1);

  const activeTab = activeIdx >= 0 && activeIdx < tabs.length ? tabs[activeIdx] : null;

  const openFile = useCallback(
    async (path: string) => {
      // If already open, just switch to it.
      const existingIdx = tabs.findIndex((t) => t.path === path);
      if (existingIdx >= 0) {
        setActiveIdx(existingIdx);
        return;
      }

      const label = path.split("/").pop() ?? path;
      const language = detectLanguage(path);

      // Add a loading tab.
      const newTab: EditorTab = {
        path,
        label,
        language,
        content: "",
        loading: true,
        error: null,
      };

      setTabs((prev) => {
        const next = [...prev, newTab];
        setActiveIdx(next.length - 1);
        return next;
      });

      // Fetch content.
      if (!projectId) return;
      try {
        const data = await api.request<FileContentResponse>(
          `/projects/${projectId}/files/content?path=${encodeURIComponent(path)}`,
        );
        setTabs((prev) =>
          prev.map((t) =>
            t.path === path
              ? { ...t, content: data.content, loading: false }
              : t,
          ),
        );
      } catch (e) {
        setTabs((prev) =>
          prev.map((t) =>
            t.path === path
              ? {
                  ...t,
                  loading: false,
                  error: e instanceof Error ? e.message : String(e),
                }
              : t,
          ),
        );
      }
    },
    [tabs, projectId, api],
  );

  const closeTab = useCallback(
    (idx: number) => {
      setTabs((prev) => {
        const next = prev.filter((_, i) => i !== idx);
        // Adjust active index.
        if (next.length === 0) {
          setActiveIdx(-1);
        } else if (idx <= activeIdx) {
          setActiveIdx(Math.max(0, activeIdx - 1));
        }
        return next;
      });
    },
    [activeIdx],
  );

  const closeAllTabs = useCallback(() => {
    setTabs([]);
    setActiveIdx(-1);
  }, []);

  return {
    tabs,
    activeTab,
    activeIdx,
    setActiveIdx,
    openFile,
    closeTab,
    closeAllTabs,
  };
}
