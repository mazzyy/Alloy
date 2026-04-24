import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "../App";
import { AuthProvider } from "../auth/AuthProvider";

// Mock fetch with URL-aware responses so the dashboard sees its own shape.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      const json = (body: unknown) =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      if (url.includes("/ping")) {
        return json({
          ok: true,
          user_id: "dev_user",
          tenant_id: "dev_tenant",
          org_role: null,
          email: null,
        });
      }
      if (url.includes("/projects")) {
        return json({ projects: [], total: 0 });
      }
      // Default: empty payload with 200 so unknown calls don't blow up.
      return json({});
    }),
  );
});

describe("App shell", () => {
  it("renders the dashboard for a dev user", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AuthProvider>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={["/"]}>
            <App />
          </MemoryRouter>
        </QueryClientProvider>
      </AuthProvider>,
    );
    // Heading is unique — multiple "Alloy" mentions exist on the page.
    expect(
      await screen.findByRole("heading", { name: /Alloy/i, level: 1 }),
    ).toBeInTheDocument();
    // Empty-state copy — confirms the project list query resolved cleanly.
    expect(
      await screen.findByText(/No projects yet/i),
    ).toBeInTheDocument();
  });
});
