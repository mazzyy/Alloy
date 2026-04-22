/**
 * Identity hook + dev-context plumbing — separated from AuthProvider.tsx so
 * the provider file exports only components (react-refresh friendly).
 */

import { createContext, useContext } from "react";
import { useAuth as useClerkAuth, useUser } from "@clerk/clerk-react";

export type AlloyIdentity = {
  isLoaded: boolean;
  isSignedIn: boolean;
  userId: string | null;
  email: string | null;
  /** Returns a bearer token for the backend (or null in dev-bootstrap). */
  getToken: () => Promise<string | null>;
};

export const DevIdentityContext = createContext<AlloyIdentity>({
  isLoaded: true,
  isSignedIn: true,
  userId: "dev_user",
  email: "dev@alloy.local",
  getToken: async () => null,
});

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
