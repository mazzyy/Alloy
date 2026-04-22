import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Sparkles, ArrowRight, Check, Loader2, AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useApi } from "@/lib/api";
import type { AppSpec } from "@alloy/shared";
import type { BuildPlan } from "@alloy/shared";

type SpecEnvelope = {
  project_id: string;
  project_slug: string;
  spec_version_id: string;
  spec_version: number;
  spec: AppSpec;
};

type PlanEnvelope = {
  project_id: string;
  project_slug: string;
  plan_version_id: string;
  plan_version: number;
  plan: BuildPlan;
};

type Status = { phase: string; [k: string]: unknown };

type Stage = "prompt" | "spec" | "plan" | "done";

const SAMPLE_PROMPT =
  "Build a lightweight task tracker for a small product team. " +
  "Users should be able to create, assign, and close tasks. " +
  "Sign-in with Clerk. No billing.";

/**
 * Phase 1 first slice: user → Spec Agent → editable spec → Planner → plan.
 *
 * Flow:
 *   1. User types a prompt and submits.
 *   2. We stream `/spec/propose` and render phase updates + final AppSpec.
 *   3. User can edit the AppSpec JSON in a textarea. Save via `/spec/save`.
 *   4. User hits "Generate Plan". We stream `/plan/build`.
 *   5. Plan renders as a grouped FileOp list.
 *
 * Deliberately not using Monaco yet — we add that with the Coder Agent
 * slice (Phase 1 wk5) where per-file diff viewing matters.
 */
export function Build() {
  const api = useApi();

  const [stage, setStage] = useState<Stage>("prompt");
  const [prompt, setPrompt] = useState(SAMPLE_PROMPT);

  const [specEnv, setSpecEnv] = useState<SpecEnvelope | null>(null);
  const [planEnv, setPlanEnv] = useState<PlanEnvelope | null>(null);

  const [specJsonText, setSpecJsonText] = useState("");
  const [specJsonError, setSpecJsonError] = useState<string | null>(null);

  const [running, setRunning] = useState<null | "spec" | "plan" | "save">(null);
  const [statusTrail, setStatusTrail] = useState<Status[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function runSpecPropose() {
    setRunning("spec");
    setError(null);
    setSpecEnv(null);
    setPlanEnv(null);
    setStatusTrail([]);
    try {
      await api.streamJson<Status, SpecEnvelope>(
        "/spec/propose",
        { method: "POST", body: JSON.stringify({ prompt }) },
        {
          onStatus: (s) => setStatusTrail((t) => [...t, s]),
          onResult: (env) => {
            setSpecEnv(env);
            setSpecJsonText(JSON.stringify(env.spec, null, 2));
            setStage("spec");
          },
          onError: (m) => setError(m),
        },
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(null);
    }
  }

  function validateSpecJson(): AppSpec | null {
    try {
      const parsed = JSON.parse(specJsonText) as AppSpec;
      if (typeof parsed !== "object" || !parsed || !parsed.slug || !parsed.name) {
        throw new Error("Missing required fields (name / slug)");
      }
      setSpecJsonError(null);
      return parsed;
    } catch (e) {
      setSpecJsonError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function saveSpec() {
    if (!specEnv) return;
    const edited = validateSpecJson();
    if (!edited) return;
    setRunning("save");
    setError(null);
    try {
      const saved = await api.request<SpecEnvelope>("/spec/save", {
        method: "POST",
        body: JSON.stringify({ project_id: specEnv.project_id, spec: edited }),
      });
      setSpecEnv(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(null);
    }
  }

  async function runPlanBuild() {
    if (!specEnv) return;
    // Save any unsaved edits first (idempotent on SHA).
    const edited = validateSpecJson();
    if (!edited) return;
    setRunning("plan");
    setError(null);
    setPlanEnv(null);
    setStatusTrail([]);
    try {
      const saved = await api.request<SpecEnvelope>("/spec/save", {
        method: "POST",
        body: JSON.stringify({ project_id: specEnv.project_id, spec: edited }),
      });
      setSpecEnv(saved);

      await api.streamJson<Status, PlanEnvelope>(
        "/plan/build",
        { method: "POST", body: JSON.stringify({ project_id: saved.project_id }) },
        {
          onStatus: (s) => setStatusTrail((t) => [...t, s]),
          onResult: (env) => {
            setPlanEnv(env);
            setStage("plan");
          },
          onError: (m) => setError(m),
        },
      );
      setStage("done");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(null);
    }
  }

  const specSummary = useMemo(() => {
    if (!specEnv) return null;
    const { entities, routes, pages, integrations } = specEnv.spec;
    return {
      entities: entities.length,
      routes: routes.length,
      pages: pages.length,
      integrations: integrations.length,
    };
  }, [specEnv]);

  const opsByPrefix = useMemo(() => {
    if (!planEnv) return null;
    const groups: Record<string, typeof planEnv.plan.ops> = {};
    for (const op of planEnv.plan.ops) {
      const key = op.id.split(".")[0] ?? "other";
      (groups[key] ??= []).push(op);
    }
    return groups;
  }, [planEnv]);

  return (
    <div className="mx-auto flex h-full max-w-5xl flex-col gap-6 p-8">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Sparkles className="h-5 w-5" />
            Build with Alloy
          </h1>
          <p className="text-sm text-muted-foreground">
            Prompt → Spec → Plan. Phase 1 first slice.
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to="/">Back to dashboard</Link>
        </Button>
      </header>

      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* 1. Prompt */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary text-xs text-primary-foreground">
              1
            </span>
            Describe the app
          </CardTitle>
          <CardDescription>
            A sentence or two is enough. You'll edit the spec before anything is built.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <textarea
            className="h-28 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            disabled={running !== null}
          />
          <div className="flex items-center gap-2">
            <Button onClick={runSpecPropose} disabled={running !== null || prompt.trim().length < 8}>
              {running === "spec" ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Proposing spec…
                </>
              ) : (
                <>
                  Propose spec <ArrowRight className="ml-2 h-4 w-4" />
                </>
              )}
            </Button>
            {statusTrail.length > 0 && running && (
              <span className="text-xs text-muted-foreground">
                {statusTrail[statusTrail.length - 1]?.phase}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* 2. Spec */}
      {specEnv && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary text-xs text-primary-foreground">
                2
              </span>
              Review the AppSpec
              <span className="ml-2 text-xs text-muted-foreground">
                v{specEnv.spec_version} · {specEnv.project_slug}
              </span>
            </CardTitle>
            <CardDescription>
              Edit freely — saved as a new version. The Planner only sees what you save.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {specSummary && (
              <div className="grid grid-cols-4 gap-2 text-xs">
                <Counter label="entities" value={specSummary.entities} />
                <Counter label="routes" value={specSummary.routes} />
                <Counter label="pages" value={specSummary.pages} />
                <Counter label="integrations" value={specSummary.integrations} />
              </div>
            )}
            <textarea
              className="h-96 w-full resize-y rounded-md border border-input bg-muted/30 px-3 py-2 font-mono text-xs"
              value={specJsonText}
              onChange={(e) => setSpecJsonText(e.target.value)}
              spellCheck={false}
              disabled={running !== null}
            />
            {specJsonError && (
              <p className="text-xs text-destructive">Invalid JSON: {specJsonError}</p>
            )}
            <div className="flex items-center gap-2">
              <Button variant="outline" onClick={saveSpec} disabled={running !== null}>
                {running === "save" ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Saving…
                  </>
                ) : (
                  <>
                    <Check className="mr-2 h-4 w-4" /> Save spec
                  </>
                )}
              </Button>
              <Button onClick={runPlanBuild} disabled={running !== null}>
                {running === "plan" ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Building plan…
                  </>
                ) : (
                  <>
                    Generate plan <ArrowRight className="ml-2 h-4 w-4" />
                  </>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 3. Plan */}
      {planEnv && opsByPrefix && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary text-xs text-primary-foreground">
                3
              </span>
              Build plan
              <span className="ml-2 text-xs text-muted-foreground">
                v{planEnv.plan_version} · {planEnv.plan.ops.length} ops · base{" "}
                {planEnv.plan.base_template}
              </span>
            </CardTitle>
            <CardDescription>
              Blocks: {planEnv.plan.blocks.length ? planEnv.plan.blocks.join(", ") : "(none)"}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {Object.entries(opsByPrefix).map(([prefix, ops]) => (
              <section key={prefix} className="space-y-1">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {prefix} · {ops.length}
                </h3>
                <ul className="divide-y divide-border rounded-md border border-border">
                  {ops.map((op) => (
                    <li
                      key={op.id}
                      className="flex items-baseline justify-between gap-4 px-3 py-2 text-xs"
                    >
                      <span className="min-w-0 flex-1">
                        <span className="font-mono text-[11px] text-muted-foreground">
                          {op.id}
                        </span>
                        <span className="ml-2">{op.intent}</span>
                      </span>
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {op.path}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
            <p className="pt-2 text-xs text-muted-foreground">
              Next: the Coder Agent will consume this plan and emit code into a per-project
              sandbox. Shipping in Phase 1 wk5.
            </p>
          </CardContent>
        </Card>
      )}

      <footer className="mt-auto text-[11px] text-muted-foreground">
        Stage: <span className="font-mono">{stage}</span>
      </footer>
    </div>
  );
}

function Counter({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-border bg-muted/30 px-2 py-1.5 text-center">
      <div className="text-base font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
    </div>
  );
}
