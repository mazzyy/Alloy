import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sparkles, Activity, LogOut } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/lib/api";
import { useIdentity } from "@/auth/identity";

type PingResponse = {
  ok: boolean;
  user_id: string;
  tenant_id: string;
  org_role: string | null;
  email: string | null;
};

export function Dashboard() {
  const api = useApi();
  const { userId, email } = useIdentity();

  const ping = useQuery({
    queryKey: ["ping"],
    queryFn: () => api.request<PingResponse>("/ping"),
  });

  const [prompt, setPrompt] = useState("Give me a five-word slogan for an AI code generator.");
  const [output, setOutput] = useState("");
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);

  async function runEcho() {
    setOutput("");
    setGenError(null);
    setGenerating(true);
    try {
      await api.stream(
        "/generate/echo",
        { method: "POST", body: JSON.stringify({ prompt, reasoning_effort: "low" }) },
        (chunk) => setOutput((o) => o + chunk),
        (err) => setGenError(err),
      );
    } catch (e) {
      setGenError(e instanceof Error ? e.message : String(e));
    } finally {
      setGenerating(false);
    }
  }

  return (
    <div className="mx-auto flex h-full max-w-5xl flex-col gap-6 p-8">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Sparkles className="h-5 w-5" />
            Alloy
          </h1>
          <p className="text-sm text-muted-foreground">
            Phase 0 shell — signed in as <span className="font-mono">{userId ?? "—"}</span>
            {email ? ` (${email})` : ""}
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <a href="/sign-in">
            <LogOut className="mr-2 h-4 w-4" /> Sign out
          </a>
        </Button>
      </header>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-4 w-4" />
              Gateway health
            </CardTitle>
            <CardDescription>
              Authenticated ping to <code>/api/v1/ping</code>.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {ping.isLoading && <p className="text-sm text-muted-foreground">Pinging…</p>}
            {ping.isError && (
              <p className="text-sm text-destructive">Error: {String(ping.error)}</p>
            )}
            {ping.data && (
              <pre className="overflow-auto rounded border border-border bg-muted/40 p-3 text-xs">
                {JSON.stringify(ping.data, null, 2)}
              </pre>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Azure OpenAI echo</CardTitle>
            <CardDescription>
              Stream a gpt-5-mini response — smoke-tests the LLM pipeline.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <textarea
              className="h-20 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
            <Button onClick={runEcho} disabled={generating || !prompt.trim()}>
              {generating ? "Streaming…" : "Run echo"}
            </Button>
            {output && (
              <pre className="whitespace-pre-wrap rounded border border-border bg-muted/40 p-3 text-xs">
                {output}
              </pre>
            )}
            {genError && <p className="text-sm text-destructive">{genError}</p>}
          </CardContent>
        </Card>
      </div>

      <footer className="mt-auto text-xs text-muted-foreground">
        Next up: Phase 1 Spec Agent → Planner → Coder Agent → Daytona preview.
      </footer>
    </div>
  );
}
