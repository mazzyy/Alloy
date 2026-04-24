/**
 * IDELayout — the main 3-panel IDE container.
 *
 * Layout:
 *   ┌───────────────────────────────────────────────────────────┐
 *   │  TopBar (project name, status, back to dashboard)        │
 *   ├────────┬──────────────────────────────┬──────────────────┤
 *   │        │                              │                  │
 *   │  File  │    Monaco Editor             │  Chat / Build    │
 *   │  Tree  │    (tabs for open files)     │  Stream Panel    │
 *   │        │                              │                  │
 *   │        ├──────────────────────────────┤                  │
 *   │        │    Preview Panel (hidden)    │                  │
 *   ├────────┴──────────────────────────────┴──────────────────┤
 *   │  Status Bar (stage, file count)                          │
 *   └─────────────────────────────────────────────────────────┘
 *
 * All panels are resizable via drag handles.
 */

import { useState, useCallback } from "react";
import { Link } from "react-router-dom";
import {
  Sparkles,
  PanelLeftClose,
  PanelLeft,
  PanelRightClose,
  PanelRight,
  ArrowLeft,
  Eye,
  EyeOff,
  Braces,
  GitBranch,
} from "lucide-react";

import { Resizable } from "@/components/ui/resizable";
import { FileTree } from "@/components/ide/FileTree";
import { EditorPanel } from "@/components/ide/EditorPanel";
import { ChatPanel } from "@/components/ide/ChatPanel";
import { PreviewPanel } from "@/components/ide/PreviewPanel";

import { useFileTree } from "@/hooks/useFileTree";
import { useEditorTabs } from "@/hooks/useEditorTabs";
import { useBuildStream } from "@/hooks/useBuildStream";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/cn";
import type { AppSpec } from "@alloy/shared";

interface IDELayoutProps {
  /** If provided, load an existing project. Otherwise start fresh. */
  initialProjectId?: string;
}

export function IDELayout({ initialProjectId }: IDELayoutProps) {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [chatOpen, setChatOpen] = useState(true);
  const [previewOpen, setPreviewOpen] = useState(false);

  /* ── Build stream ─────────────────────────────────────────── */
  const build = useBuildStream();
  const effectiveProjectId = initialProjectId ?? build.projectId;

  /* ── Editor tabs ──────────────────────────────────────────── */
  const editor = useEditorTabs(effectiveProjectId);

  /* ── File tree ────────────────────────────────────────────── */
  const fileTree = useFileTree({
    projectId: effectiveProjectId,
    onSelectFile: editor.openFile,
  });

  /* ── Spec save handler ────────────────────────────────────── */
  const handleSaveSpec = useCallback(
    (spec: AppSpec) => {
      build.saveSpec(spec);
    },
    [build],
  );

  /* ── Keyboard shortcuts ───────────────────────────────────── */
  // TODO: implement ⌘+B (toggle sidebar), ⌘+J (toggle chat)

  const projectName = build.specEnv?.spec?.name ?? "New Project";
  const projectSlug = build.specEnv?.project_slug ?? "";

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background">
      {/* ─── Top bar ─────────────────────────────────────────── */}
      <header className="flex h-[var(--topbar-height)] items-center gap-3 border-b border-border px-3">
        <Link
          to="/"
          className="flex items-center gap-1.5 text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>

        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-accent" />
          <span className="text-sm font-semibold">{projectName}</span>
          {projectSlug && (
            <span className="text-xs text-muted-foreground">
              /{projectSlug}
            </span>
          )}
        </div>

        <div className="flex-1" />

        {/* Panel toggles */}
        <button
          type="button"
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
        >
          {sidebarOpen ? (
            <PanelLeftClose className="h-4 w-4" />
          ) : (
            <PanelLeft className="h-4 w-4" />
          )}
        </button>

        <button
          type="button"
          onClick={() => setPreviewOpen(!previewOpen)}
          className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title={previewOpen ? "Hide preview" : "Show preview"}
        >
          {previewOpen ? (
            <EyeOff className="h-4 w-4" />
          ) : (
            <Eye className="h-4 w-4" />
          )}
        </button>

        <button
          type="button"
          onClick={() => setChatOpen(!chatOpen)}
          className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title={chatOpen ? "Hide chat" : "Show chat"}
        >
          {chatOpen ? (
            <PanelRightClose className="h-4 w-4" />
          ) : (
            <PanelRight className="h-4 w-4" />
          )}
        </button>
      </header>

      {/* ─── Main area ───────────────────────────────────────── */}
      <div className="flex-1 overflow-hidden">
        {/* Outer: sidebar | (editor + chat) */}
        {sidebarOpen ? (
          <Resizable
            direction="horizontal"
            defaultSize={260}
            minSize={180}
            maxSize={400}
          >
            {/* Sidebar */}
            <FileTree
              flatTree={fileTree.flatTree}
              activePath={editor.activeTab?.path ?? null}
              onSelectFile={fileTree.selectFile}
              onToggleDir={fileTree.toggleDir}
              onRefresh={fileTree.refreshTree}
              isLoading={fileTree.isLoading}
              noWorkspace={fileTree.noWorkspace}
              className="border-r border-border bg-card/30"
            />

            {/* Editor + Chat */}
            <EditorChatArea
              editor={editor}
              build={build}
              chatOpen={chatOpen}
              previewOpen={previewOpen}
              onSaveSpec={handleSaveSpec}
            />
          </Resizable>
        ) : (
          <EditorChatArea
            editor={editor}
            build={build}
            chatOpen={chatOpen}
            previewOpen={previewOpen}
            onSaveSpec={handleSaveSpec}
          />
        )}
      </div>

      {/* ─── Status bar ──────────────────────────────────────── */}
      <footer className="flex h-[var(--statusbar-height)] items-center gap-4 border-t border-border bg-card/30 px-3 text-[11px] text-muted-foreground">
        <div className="flex items-center gap-1.5">
          <Braces className="h-3 w-3" />
          <span>
            {editor.tabs.length} file{editor.tabs.length !== 1 ? "s" : ""} open
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <GitBranch className="h-3 w-3" />
          <span>main</span>
        </div>
        <div className="flex-1" />
        <Badge
          variant={
            build.stage === "done"
              ? "success"
              : build.stage === "error"
                ? "destructive"
                : build.stage === "idle"
                  ? "default"
                  : "accent"
          }
        >
          {build.stage}
        </Badge>
      </footer>
    </div>
  );
}

/* ── Editor + Chat inner layout ──────────────────────────────── */

interface EditorChatAreaProps {
  editor: ReturnType<typeof useEditorTabs>;
  build: ReturnType<typeof useBuildStream>;
  chatOpen: boolean;
  previewOpen: boolean;
  onSaveSpec: (spec: AppSpec) => void;
}

function EditorChatArea({
  editor,
  build,
  chatOpen,
  previewOpen,
  onSaveSpec,
}: EditorChatAreaProps) {
  const editorContent = previewOpen ? (
    <Resizable direction="vertical" defaultSize={400} minSize={200} maxSize={800}>
      <EditorPanel
        tabs={editor.tabs}
        activeIdx={editor.activeIdx}
        onSelectTab={editor.setActiveIdx}
        onCloseTab={editor.closeTab}
      />
      <PreviewPanel previewUrl={null} />
    </Resizable>
  ) : (
    <EditorPanel
      tabs={editor.tabs}
      activeIdx={editor.activeIdx}
      onSelectTab={editor.setActiveIdx}
      onCloseTab={editor.closeTab}
    />
  );

  if (!chatOpen) {
    return editorContent;
  }

  return (
    <div className="flex h-full">
      <div className="min-w-0 flex-1">{editorContent}</div>
      <div
        className={cn(
          "h-full flex-shrink-0 border-l border-border",
        )}
        style={{ width: "var(--chat-width)" }}
      >
        <ChatPanel
          stage={build.stage}
          messages={build.messages}
          specJsonText={build.specJsonText}
          onSpecJsonChange={build.setSpecJsonText}
          onProposeSpec={build.proposeSpec}
          onSaveSpec={onSaveSpec}
          onGeneratePlan={build.generatePlan}
        />
      </div>
    </div>
  );
}
