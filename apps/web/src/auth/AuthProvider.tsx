/**
 * AuthProvider — wraps the app in ClerkProvider when VITE_CLERK_PUBLISHABLE_KEY
 * is set, otherwise falls back to a dev-only identity context so the shell
 * renders during Phase 0 before Clerk is provisioned.
 *
 * This mirrors the backend's local-bootstrap behavior (see app/api/deps.py):
 * if neither VITE_CLERK_PUBLISHABLE_KEY nor CLERK_ISSUER is set, the UI
 * shows as "dev_user" and /api/v1/ping returns dev_user/dev_tenant.
 */

import { createContext, useContext, type PropsWithChildren } from "react";
import { ClerkProvider, useAuth as useClerkAuth, useUser } from "@clerk/clerk-react";

type AlloyIdentity = {
  isLoaded: boolean;
  isSignedIn: boolean;
  userId: string | null;
  email: string | null;
  /** Returns a bearer token for the backend (or null in dev-bootstrap). */
  getToken: () => Promise<string | null>;
};

const DevIdentityContext = createContext<AlloyIdentity>({
  isLoaded: true,
  isSignedIn: true,
  userId: "dev_user",
  email: "dev@alloy.local",
  getToken: async () => null,
});

function DevAuthShell({ children }: PropsWithChildren) {
  return <DevIdentityContext.Provider value={useContext(DevIdentityContext)}>{children}</DevIdentityContext.Provider>;
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

export function useIdentity(): AlloyIdentity {
  const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;
  if (!publishableKey) {
    // eslint-disable-next-line react-hooks/rules-of-hooks
    return useContext(DevIdentityContext);
  }
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const clerk = useClerkAuth();
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const { user } = useUser();
  return {
    isLoaded: clerk.isLoaded,
    isSignedIn: !!clerk.isSignedIn,
    userId: clerk.userId ?? null,
    email: user?.primaryEmailAddress?.emailAddress ?? null,
    getToken: () => clerk.getToken(),
  };
}
