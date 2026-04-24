/**
 * EditorPanel — Monaco editor with multi-tab support.
 *
 * Read-only in Phase 1 — edits go through the Coder Agent.
 * Uses @monaco-editor/react which is already in package.json.
 */

import Editor from "@monaco-editor/react";
import { TabBar, Tab } from "@/components/ui/tabs";
import { FileCode2, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import type { EditorTab } from "@/hooks/useEditorTabs";

interface EditorPanelProps {
  tabs: EditorTab[];
  activeIdx: number;
  onSelectTab: (idx: number) => void;
  onCloseTab: (idx: number) => void;
  className?: string;
}

export function EditorPanel({
  tabs,
  activeIdx,
  onSelectTab,
  onCloseTab,
  className,
}: EditorPanelProps) {
  const activeTab = activeIdx >= 0 && activeIdx < tabs.length ? tabs[activeIdx] : null;

  return (
    <div className={cn("flex h-full flex-col", className)}>
      {/* Tab bar */}
      {tabs.length > 0 && (
        <TabBar>
          {tabs.map((tab, idx) => (
            <Tab
              key={tab.path}
              active={idx === activeIdx}
              onClick={() => onSelectTab(idx)}
              onClose={() => onCloseTab(idx)}
              icon={<FileCode2 className="h-3 w-3" />}
            >
              {tab.label}
            </Tab>
          ))}
        </TabBar>
      )}

      {/* Editor area */}
      <div className="relative flex-1">
        {!activeTab && (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
            <FileCode2 className="h-12 w-12 opacity-20" />
            <p className="text-sm">Select a file to view</p>
            <p className="text-xs opacity-60">
              Open files from the explorer sidebar
            </p>
          </div>
        )}

        {activeTab?.loading && (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-accent" />
          </div>
        )}

        {activeTab?.error && (
          <div className="flex h-full items-center justify-center p-8">
            <p className="max-w-sm text-center text-sm text-destructive">
              {activeTab.error}
            </p>
          </div>
        )}

        {activeTab && !activeTab.loading && !activeTab.error && (
          <Editor
            height="100%"
            language={activeTab.language}
            value={activeTab.content}
            theme="vs-dark"
            options={{
              readOnly: true,
              minimap: { enabled: false },
              fontSize: 13,
              fontFamily: "'JetBrains Mono', monospace",
              lineNumbers: "on",
              scrollBeyondLastLine: false,
              renderWhitespace: "selection",
              bracketPairColorization: { enabled: true },
              padding: { top: 8 },
              scrollbar: {
                verticalScrollbarSize: 6,
                horizontalScrollbarSize: 6,
              },
              overviewRulerBorder: false,
              hideCursorInOverviewRuler: true,
              wordWrap: "off",
            }}
          />
        )}
      </div>
    </div>
  );
}
