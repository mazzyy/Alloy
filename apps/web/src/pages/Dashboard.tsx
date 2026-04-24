/**
 * Dashboard — project list and creation entry point.
 *
 * Shows the user's projects in a card grid with status indicators.
 * "New Project" navigates to `/build` for a fresh start.
 * Clicking an existing project navigates to `/build/{project_id}`.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  Sparkles,
  Plus,
  FolderOpen,
  Braces,
  Map,
  Clock,
  LogOut,
  Zap,
  Loader2,
  AlertCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useApi } from "@/lib/api";
import { useIdentity } from "@/auth/identity";

interface ProjectSummary {
  id: string;
  slug: string;
  name: string;
  status: string;
  has_spec: boolean;
  has_plan: boolean;
  created_at: string;
  updated_at: string;
}

interface ProjectListResponse {
  projects: ProjectSummary[];
  total: number;
}

export function Dashboard() {
  const api = useApi();
  const { email } = useIdentity();
  const navigate = useNavigate();

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.request<ProjectListResponse>("/projects?limit=50"),
    retry: 1,
  });

  return (
    <div className="flex h-full flex-col overflow-auto">
      {/* ── Hero header ─────────────────────────────────────── */}
      <header className="relative overflow-hidden border-b border-border">
        {/* Gradient background */}
        <div className="absolute inset-0 bg-gradient-to-br from-accent/10 via-background to-background" />
        <div className="absolute -right-20 -top-20 h-64 w-64 rounded-full bg-accent/5 blur-3xl" />
        <div className="absolute -left-10 bottom-0 h-40 w-40 rounded-full bg-success/5 blur-3xl" />

        <div className="relative mx-auto flex max-w-6xl items-center justify-between px-6 py-8">
          <div>
            <h1 className="flex items-center gap-2.5 text-2xl font-bold tracking-tight">
              <Sparkles className="h-6 w-6 text-accent" />
              Alloy
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              AI full-stack generator · React + FastAPI + Postgres
            </p>
            {email && (
              <p className="mt-0.5 text-xs text-muted-foreground/60">
                {email}
              </p>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Button onClick={() => navigate("/build")} className="glow-accent">
              <Plus className="mr-2 h-4 w-4" />
              New Project
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <a href="/sign-in">
                <LogOut className="mr-1.5 h-3.5 w-3.5" />
                Sign out
              </a>
            </Button>
          </div>
        </div>
      </header>

      {/* ── Project grid ────────────────────────────────────── */}
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
        <div className="mb-6 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
            Your Projects
          </h2>
          {projects.data && (
            <span className="text-xs text-muted-foreground">
              {projects.data.total} project
              {projects.data.total !== 1 ? "s" : ""}
            </span>
          )}
        </div>

        {/* Loading */}
        {projects.isLoading && (
          <div className="flex items-center gap-2 py-12 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="text-sm">Loading projects…</span>
          </div>
        )}

        {/* Error */}
        {projects.isError && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 flex-shrink-0" />
            <span>
              Failed to load projects.{" "}
              <button
                type="button"
                onClick={() => projects.refetch()}
                className="underline"
              >
                Retry
              </button>
            </span>
          </div>
        )}

        {/* Empty state */}
        {projects.data && projects.data.projects.length === 0 && (
          <div className="flex flex-col items-center gap-4 py-20 text-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-accent/10">
              <Zap className="h-8 w-8 text-accent" />
            </div>
            <div>
              <p className="text-lg font-semibold">No projects yet</p>
              <p className="mt-1 max-w-sm text-sm text-muted-foreground">
                Describe an app in plain English and Alloy will generate a
                full-stack React + FastAPI project for you.
              </p>
            </div>
            <Button onClick={() => navigate("/build")} className="glow-accent">
              <Plus className="mr-2 h-4 w-4" />
              Create your first project
            </Button>
          </div>
        )}

        {/* Project cards */}
        {projects.data && projects.data.projects.length > 0 && (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {projects.data.projects.map((project) => (
              <Link
                key={project.id}
                to={`/build/${project.id}`}
                className="group relative overflow-hidden rounded-lg border border-border bg-card/50 p-5 transition-all duration-200 hover:border-accent/30 hover:bg-card/80 hover:shadow-lg hover:shadow-accent/5"
              >
                {/* Hover glow */}
                <div className="absolute -right-8 -top-8 h-24 w-24 rounded-full bg-accent/5 opacity-0 blur-2xl transition-opacity group-hover:opacity-100" />

                <div className="relative">
                  <div className="flex items-start justify-between">
                    <div className="min-w-0 flex-1">
                      <h3 className="truncate font-semibold">{project.name}</h3>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        /{project.slug}
                      </p>
                    </div>
                    <FolderOpen className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
                  </div>

                  <div className="mt-4 flex items-center gap-2">
                    <Badge
                      variant={project.has_spec ? "success" : "default"}
                    >
                      <Braces className="mr-1 h-2.5 w-2.5" />
                      Spec
                    </Badge>
                    <Badge
                      variant={project.has_plan ? "success" : "default"}
                    >
                      <Map className="mr-1 h-2.5 w-2.5" />
                      Plan
                    </Badge>
                  </div>

                  <div className="mt-3 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                    <Clock className="h-3 w-3" />
                    {(() => {
                      const d = new Date(project.updated_at);
                      return isNaN(d.getTime())
                        ? "—"
                        : d.toLocaleDateString(undefined, {
                            month: "short",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                          });
                    })()}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>

      {/* ── Footer ──────────────────────────────────────────── */}
      <footer className="border-t border-border px-6 py-3 text-center text-[11px] text-muted-foreground">
        Alloy · Phase 1 · React + FastAPI + Postgres
      </footer>
    </div>
  );
}
