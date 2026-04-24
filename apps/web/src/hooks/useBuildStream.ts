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

  /* ── Reset ───────────────────────────────────────────────────── */

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setStage("idle");
    setMessages([]);
    setSpecEnv(null);
    setPlanEnv(null);
    setProjectId(null);
    setSpecJsonText("");
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
    reset,
    addMessage,
  };
}
