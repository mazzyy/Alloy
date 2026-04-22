/**
 * AuthProvider — wraps the app in ClerkProvider when VITE_CLERK_PUBLISHABLE_KEY
 * is set, otherwise falls back to a dev-only identity context so the shell
 * renders during Phase 0 before Clerk is provisioned.
 *
 * This mirrors the backend's local-bootstrap behavior (see app/api/deps.py):
 * if neither VITE_CLERK_PUBLISHABLE_KEY nor CLERK_ISSUER is set, the UI
 * shows as "dev_user" and /api/v1/ping returns dev_user/dev_tenant.
 */

import { useContext, type PropsWithChildren } from "react";
import { ClerkProvider } from "@clerk/clerk-react";
import { DevIdentityContext } from "./identity";

function DevAuthShell({ children }: PropsWithChildren) {
  return (
    <DevIdentityContext.Provider value={useContext(DevIdentityContext)}>
      {children}
    </DevIdentityContext.Provider>
  );
}

export function AuthProvider({ children }: PropsWithChildren) {
  const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;
  if (!publishableKey) {
    return <DevAuthShell>{children}</DevAuthShell>;
  }
  return (
    <ClerkProvider publishableKey={publishableKey} afterSignOutUrl="/">
      {children}
    </ClerkProvider>
  );
}
