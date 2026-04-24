/**
 * ChatPanel — right-hand panel for the build conversation.
 *
 * Renders the streaming build progress: user prompts, spec agent phases,
 * plan generation, and tool events. Also hosts the prompt input and
 * action buttons (propose spec, generate plan).
 */

import { useRef, useEffect, useState, type ReactNode } from "react";
import {
  Sparkles,
  Send,
  Loader2,
  User,
  Bot,
  AlertCircle,
  Info,
  ChevronDown,
  ChevronRight,
  Check,
  ArrowRight,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/cn";
import type { ChatMessage, BuildStage } from "@/hooks/useBuildStream";
import type { AppSpec } from "@alloy/shared";

/* ── Props ─────────────────────────────────────────────────────── */

interface ChatPanelProps {
  stage: BuildStage;
  messages: ChatMessage[];
  specJsonText: string;
  onSpecJsonChange: (text: string) => void;
  onProposeSpec: (prompt: string) => void;
  onSaveSpec: (spec: AppSpec) => void;
  onGeneratePlan: () => void;
  onRunBuild?: () => void;
  onResumeBuild?: (answer: string) => void;
  pendingReviewQuestion?: string | null;
  pendingReviewOptions?: string[];
  className?: string;
}

/* ── Message renderer ──────────────────────────────────────────── */

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const [expanded, setExpanded] = useState(false);

  const roleIcons: Record<string, ReactNode> = {
    user: <User className="h-3.5 w-3.5" />,
    assistant: <Bot className="h-3.5 w-3.5" />,
    system: <Info className="h-3.5 w-3.5" />,
    tool: <Sparkles className="h-3.5 w-3.5" />,
    error: <AlertCircle className="h-3.5 w-3.5" />,
  };
  const roleIcon = roleIcons[msg.role];

  const roleBgs: Record<string, string> = {
    user: "bg-accent/10 border-accent/20",
    assistant: "bg-success/5 border-success/15",
    system: "bg-muted/50 border-border",
    tool: "bg-info/5 border-info/15",
    error: "bg-destructive/10 border-destructive/20",
  };
  const roleBg = roleBgs[msg.role];

  const roleColors: Record<string, string> = {
    user: "text-accent",
    assistant: "text-success",
    system: "text-muted-foreground",
    tool: "text-info",
    error: "text-destructive",
  };
  const roleColor = roleColors[msg.role];

  return (
    <div className={cn("rounded-md border px-3 py-2", roleBg)}>
      <div className="flex items-start gap-2">
        <span className={cn("mt-0.5 flex-shrink-0", roleColor)}>
          {roleIcon}
        </span>
        <div className="min-w-0 flex-1">
          {msg.label && (
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              {msg.label}
            </span>
          )}
          <p className="text-xs leading-relaxed whitespace-pre-wrap">{msg.content}</p>
          {msg.data != null && (
            <button
              type="button"
              onClick={() => setExpanded(!expanded)}
              className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
            >
              {expanded ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              View data
            </button>
          )}
          {expanded && msg.data != null && (
            <pre className="mt-1 max-h-60 overflow-auto rounded border border-border bg-background/50 p-2 text-[10px] leading-relaxed">
              {JSON.stringify(msg.data, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Main component ────────────────────────────────────────────── */

export function ChatPanel({
  stage,
  messages,
  specJsonText,
  onSpecJsonChange,
  onProposeSpec,
  onSaveSpec,
  onGeneratePlan,
  onRunBuild,
  onResumeBuild,
  pendingReviewQuestion,
  pendingReviewOptions,
  className,
}: ChatPanelProps) {
  const [reviewAnswer, setReviewAnswer] = useState("");
  const [promptText, setPromptText] = useState(
    "Build a lightweight task tracker for a small product team. " +
      "Users should be able to create, assign, and close tasks. " +
      "Sign-in with Clerk. No billing.",
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const [specError, setSpecError] = useState<string | null>(null);

  // Auto-scroll to bottom on new messages.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length]);

  const isRunning =
    stage === "proposing_spec" || stage === "planning" || stage === "building";

  function handleSubmitPrompt() {
    if (!promptText.trim() || isRunning) return;
    onProposeSpec(promptText.trim());
  }

  function handleSaveSpec() {
    try {
      const parsed = JSON.parse(specJsonText) as AppSpec;
      setSpecError(null);
      onSaveSpec(parsed);
    } catch (e) {
      setSpecError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className={cn("flex h-full flex-col", className)}>
      {/* Header */}
      <div className="flex h-[var(--tab-height)] items-center justify-between border-b border-border px-3">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          <Sparkles className="h-3.5 w-3.5 text-accent" />
          Build
        </span>
        <Badge
          variant={
            stage === "done"
              ? "success"
              : stage === "error"
                ? "destructive"
                : isRunning
                  ? "accent"
                  : "default"
          }
        >
          {stage}
        </Badge>
      </div>

      {/* Message stream */}
      <ScrollArea ref={scrollRef} className="flex-1 p-3">
        <div className="space-y-2">
          {messages.map((msg) => (
            <MessageBubble key={msg.id} msg={msg} />
          ))}

          {isRunning && (
            <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Processing…
            </div>
          )}
        </div>
      </ScrollArea>

      {/* Spec editor (when in editing_spec stage) */}
      {stage === "editing_spec" && (
        <div className="border-t border-border p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[11px] font-medium text-muted-foreground">
              AppSpec (editable JSON)
            </span>
            <div className="flex gap-1.5">
              <Button variant="outline" size="sm" onClick={handleSaveSpec}>
                <Check className="mr-1 h-3 w-3" /> Save
              </Button>
              <Button size="sm" onClick={onGeneratePlan}>
                Plan <ArrowRight className="ml-1 h-3 w-3" />
              </Button>
            </div>
          </div>
          <textarea
            className="h-48 w-full resize-y rounded-md border border-input bg-background px-3 py-2 font-mono text-[11px] leading-relaxed"
            value={specJsonText}
            onChange={(e) => onSpecJsonChange(e.target.value)}
            spellCheck={false}
          />
          {specError && (
            <p className="mt-1 text-[10px] text-destructive">{specError}</p>
          )}
        </div>
      )}

      {/* Plan complete — fire the Coder Agent */}
      {stage === "done" && onRunBuild && (
        <div className="border-t border-border p-3">
          <Button
            className="w-full"
            size="sm"
            onClick={onRunBuild}
          >
            <Sparkles className="mr-2 h-3.5 w-3.5" />
            Build with Coder Agent
          </Button>
          <p className="mt-1.5 text-center text-[10px] text-muted-foreground">
            Runs every plan task through the Coder Agent + validators,
            then exports OpenAPI and regenerates the TS client.
          </p>
        </div>
      )}

      {/* Paused on `request_human_review` — collect the answer */}
      {stage === "needs_review" && onResumeBuild && (
        <div className="border-t border-border p-3">
          <p className="mb-2 text-xs font-medium">
            Coder Agent paused for review
          </p>
          {pendingReviewQuestion && (
            <p className="mb-2 text-[11px] leading-relaxed text-muted-foreground">
              {pendingReviewQuestion}
            </p>
          )}
          {pendingReviewOptions && pendingReviewOptions.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1">
              {pendingReviewOptions.map((opt) => (
                <Button
                  key={opt}
                  variant="outline"
                  size="sm"
                  onClick={() => onResumeBuild(opt)}
                >
                  {opt}
                </Button>
              ))}
            </div>
          )}
          <textarea
            className="mb-2 h-20 w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-xs"
            placeholder="Or type your answer…"
            value={reviewAnswer}
            onChange={(e) => setReviewAnswer(e.target.value)}
          />
          <Button
            className="w-full"
            size="sm"
            disabled={!reviewAnswer.trim()}
            onClick={() => {
              onResumeBuild(reviewAnswer.trim());
              setReviewAnswer("");
            }}
          >
            Resume build
          </Button>
        </div>
      )}

      {/* Prompt input (when idle or after error) */}
      {(stage === "idle" || stage === "error") && (
        <div className="border-t border-border p-3">
          <div className="flex gap-2">
            <textarea
              className="flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              rows={3}
              placeholder="Describe the app you want to build…"
              value={promptText}
              onChange={(e) => setPromptText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  handleSubmitPrompt();
                }
              }}
            />
            <Button
              onClick={handleSubmitPrompt}
              disabled={!promptText.trim() || isRunning}
              className="self-end"
            >
              <Send className="h-4 w-4" />
            </Button>
          </div>
          <p className="mt-1 text-[10px] text-muted-foreground">
            ⌘ + Enter to submit
          </p>
        </div>
      )}
    </div>
  );
}
