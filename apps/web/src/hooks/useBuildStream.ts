/**
 * useBuildStream — orchestrates the build lifecycle state machine.
 *
 * States: idle → proposing_spec → editing_spec → planning → done | error
 *
 * Each phase streams from the corresponding API endpoint and emits
 * structured ChatMessage objects for the ChatPanel to render.
 */

import { useState, useCallback, useRef } from "react";
import { useApi } from "@/lib/api";
import type { AppSpec, BuildPlan } from "@alloy/shared";

/* ── Message types ───────────────────────────────────────────────── */

export type MessageRole = "user" | "system" | "assistant" | "tool" | "error";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  /** Optional structured data for rich rendering. */
  data?: unknown;
  /** Tool/phase label, e.g. "spec_agent", "planner", "scaffold". */
  label?: string;
  timestamp: number;
}

/* ── Build stage ─────────────────────────────────────────────────── */

export type BuildStage =
  | "idle"
  | "proposing_spec"
  | "editing_spec"
  | "planning"
  | "building"
  | "needs_review"
  | "done"
  | "error";

/* ── Envelope types (matching backend) ───────────────────────────── */

interface SpecEnvelope {
  project_id: string;
  project_slug: string;
  spec_version_id: string;
  spec_version: number;
  spec: AppSpec;
}

interface PlanEnvelope {
  project_id: string;
  project_slug: string;
  plan_version_id: string;
  plan_version: number;
  plan: BuildPlan;
}

interface BuildTaskOutcome {
  task_id: string;
  ok: boolean;
  attempts_used: number;
  commit_sha?: string | null;
  summary?: string;
  error?: string | null;
}

interface BuildResultEnvelope {
  run_id: string;
  thread_id: string;
  ok: boolean;
  tasks_run: number;
  tasks_total: number;
  outcomes: BuildTaskOutcome[];
  pending_review: {
    task_id: string;
    question: string;
    options: string[];
  } | null;
  finalise: {
    ok: boolean;
    steps: Array<{
      name: string;
      ok: boolean;
      skipped: boolean;
      return_code: number | null;
      error: string | null;
    }>;
  } | null;
}

interface StatusEvent {
  phase: string;
  [k: string]: unknown;
}

/* ── Hook ────────────────────────────────────────────────────────── */

let msgCounter = 0;
function makeId(): string {
  return `msg_${++msgCounter}_${Date.now()}`;
}

export function useBuildStream() {
  const api = useApi();

  const [stage, setStage] = useState<BuildStage>("idle");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [specEnv, setSpecEnv] = useState<SpecEnvelope | null>(null);
  const [planEnv, setPlanEnv] = useState<PlanEnvelope | null>(null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [specJsonText, setSpecJsonText] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const addMessage = useCallback(
    (role: MessageRole, content: string, label?: string, data?: unknown) => {
      setMessages((prev) => [
        ...prev,
        { id: makeId(), role, content, label, data, timestamp: Date.now() },
      ]);
    },
    [],
  );

  /* ── Propose spec ────────────────────────────────────────────── */

  const proposeSpec = useCallback(
    async (prompt: string) => {
      setStage("proposing_spec");
      addMessage("user", prompt);
      addMessage("system", "Running Spec Agent…", "spec_agent");

      try {
        await api.streamJson<StatusEvent, SpecEnvelope>(
          "/spec/propose",
          { method: "POST", body: JSON.stringify({ prompt }) },
          {
            onStatus: (s) => {
              addMessage("system", `Phase: ${s.phase}`, "spec_agent");
            },
            onResult: (env) => {
              setSpecEnv(env);
              setProjectId(env.project_id);
              setSpecJsonText(JSON.stringify(env.spec, null, 2));
              addMessage(
                "assistant",
                `Spec proposed for **${env.spec.name}** (${env.spec.entities.length} entities, ${env.spec.routes.length} routes, ${env.spec.pages.length} pages).`,
                "spec_agent",
                env.spec,
              );
              setStage("editing_spec");
            },
            onError: (err) => {
              addMessage("error", err, "spec_agent");
              setStage("error");
            },
          },
        );
      } catch (e) {
        addMessage(
          "error",
          e instanceof Error ? e.message : String(e),
          "spec_agent",
        );
        setStage("error");
      }
    },
    [api, addMessage],
  );

  /* ── Save spec ───────────────────────────────────────────────── */

  const saveSpec = useCallback(
    async (editedSpec: AppSpec) => {
      if (!specEnv) return;
      try {
        const saved = await api.request<SpecEnvelope>("/spec/save", {
          method: "POST",
          body: JSON.stringify({
            project_id: specEnv.project_id,
            spec: editedSpec,
          }),
        });
        setSpecEnv(saved);
        addMessage(
          "system",
          `Spec saved (v${saved.spec_version}).`,
          "spec_agent",
        );
      } catch (e) {
        addMessage(
          "error",
          e instanceof Error ? e.message : String(e),
          "spec_save",
        );
      }
    },
    [api, specEnv, addMessage],
  );

  /* ── Generate plan ───────────────────────────────────────────── */

  const generatePlan = useCallback(async () => {
    if (!specEnv) return;

    // Auto-save any edits first.
    try {
      const edited = JSON.parse(specJsonText) as AppSpec;
      await saveSpec(edited);
    } catch {
      // If JSON is invalid, use the last saved spec.
    }

    setStage("planning");
    addMessage("system", "Running Planner Agent…", "planner");

    try {
      await api.streamJson<StatusEvent, PlanEnvelope>(
        "/plan/build",
        {
          method: "POST",
          body: JSON.stringify({ project_id: specEnv.project_id }),
        },
        {
          onStatus: (s) => {
            addMessage("system", `Phase: ${s.phase}`, "planner");
          },
          onResult: (env) => {
            setPlanEnv(env);
            addMessage(
              "assistant",
              `Build plan generated: ${env.plan.ops.length} file operations, base \`${env.plan.base_template}\`, blocks: ${env.plan.blocks.length ? env.plan.blocks.join(", ") : "(none)"}.`,
              "planner",
              env.plan,
            );
            setStage("done");
          },
          onError: (err) => {
            addMessage("error", err, "planner");
            setStage("error");
          },
        },
      );
    } catch (e) {
      addMessage(
        "error",
        e instanceof Error ? e.message : String(e),
        "planner",
      );
      setStage("error");
    }
  }, [api, specEnv, specJsonText, saveSpec, addMessage]);

  /* ── Run build (Coder Agent → workspace writes) ──────────────── */

  const [buildResult, setBuildResult] = useState<BuildResultEnvelope | null>(
    null,
  );

  const runBuild = useCallback(async () => {
    if (!planEnv) return;

    setStage("building");
    setBuildResult(null);
    addMessage("system", "Running Coder Agent…", "build");

    try {
      await api.streamJson<StatusEvent, BuildResultEnvelope>(
        "/build/run",
        {
          method: "POST",
          body: JSON.stringify({
            project_id: planEnv.project_id,
            plan_version_id: planEnv.plan_version_id,
          }),
        },
        {
          onStatus: (s) => {
            // Render the most useful sub-events as readable lines and
            // hide the noisy ones behind data-only messages.
            if (s.phase === "task_finished") {
              const ok = s.ok as boolean;
              const id = s.task_id as string;
              const idx = (s.idx as number) + 1;
              const total = s.total as number;
              addMessage(
                ok ? "tool" : "error",
                `${ok ? "✓" : "✗"} [${idx}/${total}] ${id}`,
                "coder",
                s,
              );
            } else if (s.phase === "scaffolded") {
              addMessage(
                "system",
                `Workspace ready (sandbox ${s.sandbox_id ?? "(none)"}).`,
                "build",
              );
            } else if (s.phase === "build_started") {
              addMessage(
                "system",
                `Build started — ${s.tasks_total} tasks queued.`,
                "build",
              );
            } else if (s.phase === "finalise_started") {
              addMessage(
                "system",
                "Running post-build finalisation (alembic + openapi + ts client)…",
                "finalise",
              );
            } else if (s.phase === "finalise_finished") {
              const stepsRaw = s.steps as
                | Array<{ name: string; ok: boolean; skipped: boolean }>
                | undefined;
              const stepLine =
                stepsRaw
                  ?.map(
                    (st) =>
                      `${st.skipped ? "·" : st.ok ? "✓" : "✗"} ${st.name}`,
                  )
                  .join("  ") ?? "(no steps)";
              addMessage(
                s.ok ? "tool" : "error",
                `Finalisation ${s.ok ? "complete" : "had failures"}: ${stepLine}`,
                "finalise",
                s,
              );
            } else if (s.phase === "finalise_crashed") {
              addMessage(
                "error",
                `Finalisation crashed: ${s.error ?? "(unknown)"}`,
                "finalise",
              );
            } else {
              addMessage("system", `Phase: ${s.phase}`, "build", s);
            }
          },
          onResult: (env) => {
            setBuildResult(env);
            const tail = env.pending_review
              ? `Paused for review: ${env.pending_review.question}`
              : env.ok
                ? `Build green — ${env.tasks_run}/${env.tasks_total} tasks succeeded.`
                : `Build failed — ${env.tasks_run}/${env.tasks_total} tasks attempted.`;
            addMessage(
              env.ok ? "assistant" : "error",
              tail,
              "coder",
              env,
            );
            setStage(
              env.pending_review ? "needs_review" : env.ok ? "done" : "error",
            );
          },
          onError: (err) => {
            addMessage("error", err, "build");
            setStage("error");
          },
        },
      );
    } catch (e) {
      addMessage(
        "error",
        e instanceof Error ? e.message : String(e),
        "build",
      );
      setStage("error");
    }
  }, [api, planEnv, addMessage]);

  /* ── Resume build (after needs_review) ───────────────────────── */

  const resumeBuild = useCallback(
    async (answer: string) => {
      if (!buildResult || !buildResult.run_id) return;
      setStage("building");
      addMessage("user", answer, "review");

      try {
        await api.streamJson<StatusEvent, BuildResultEnvelope>(
          "/build/resume",
          {
            method: "POST",
            body: JSON.stringify({
              run_id: buildResult.run_id,
              answer,
            }),
          },
          {
            onStatus: (s) => {
              addMessage("system", `Phase: ${s.phase}`, "build", s);
            },
            onResult: (env) => {
              setBuildResult(env);
              addMessage(
                env.ok ? "assistant" : "error",
                env.ok
                  ? `Resumed and built green.`
                  : `Resumed but build failed.`,
                "coder",
                env,
              );
              setStage(
                env.pending_review ? "needs_review" : env.ok ? "done" : "error",
              );
            },
            onError: (err) => {
              addMessage("error", err, "build");
              setStage("error");
            },
          },
        );
      } catch (e) {
        addMessage(
          "error",
          e instanceof Error ? e.message : String(e),
          "build",
        );
        setStage("error");
      }
    },
    [api, buildResult, addMessage],
  );

  /* ── Reset ───────────────────────────────────────────────────── */

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setStage("idle");
    setMessages([]);
    setSpecEnv(null);
    setPlanEnv(null);
    setProjectId(null);
    setSpecJsonText("");
    setBuildResult(null);
  }, []);

  return {
    stage,
    messages,
    specEnv,
    planEnv,
    projectId,
    specJsonText,
    setSpecJsonText,
    proposeSpec,
    saveSpec,
    generatePlan,
    runBuild,
    resumeBuild,
    buildResult,
    reset,
    addMessage,
  };
}
