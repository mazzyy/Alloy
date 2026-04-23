/**
 * Clerk provider + `useIdentity` hook. Mirrors Alloy's own `auth/identity` so
 * generated apps feel consistent with the Alloy IDE they came from.
 *
 * Wrap the app tree in `<ClerkAuthProvider>` and call `useIdentity()` from any
 * component. `getToken()` lazily returns a fresh session token to pass as the
 * `Authorization: Bearer ...` header to the generated FastAPI backend.
 */

import type { ReactNode } from "react";
import { ClerkProvider, useAuth, useUser } from "@clerk/clerk-react";

const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string;
if (!publishableKey) {
  throw new Error(
    "VITE_CLERK_PUBLISHABLE_KEY is not set. See .env.example for details.",
  );
}

export function ClerkAuthProvider({ children }: { children: ReactNode }) {
  return <ClerkProvider publishableKey={publishableKey}>{children}</ClerkProvider>;
}

export function useIdentity() {
  const { isLoaded, isSignedIn, getToken, signOut } = useAuth();
  const { user } = useUser();
  return {
    isLoaded,
    isSignedIn: Boolean(isSignedIn),
    userId: user?.id ?? null,
    email: user?.primaryEmailAddress?.emailAddress ?? null,
    getToken: async () => (isSignedIn ? await getToken() : null),
    signOut,
  };
}
