import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "../App";
import { AuthProvider } from "../auth/AuthProvider";

// Mock fetch to return a deterministic ping payload.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            ok: true,
            user_id: "dev_user",
            tenant_id: "dev_tenant",
            org_role: null,
            email: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    ),
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
    expect(await screen.findByText(/Alloy/i)).toBeInTheDocument();
    expect(await screen.findByText(/Gateway health/i)).toBeInTheDocument();
  });
});
