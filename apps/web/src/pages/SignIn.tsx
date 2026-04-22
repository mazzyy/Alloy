import { SignIn as ClerkSignIn } from "@clerk/clerk-react";

/**
 * When Clerk is configured, render Clerk's pre-built sign-in component.
 * When not configured, the DevAuthShell already reports `isSignedIn = true`
 * so we never reach this page in dev bootstrap mode.
 */
export function SignIn() {
  const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;
  if (!publishableKey) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <h1 className="text-xl font-semibold">Clerk not configured</h1>
        <p className="max-w-md text-sm text-muted-foreground">
          Set <code className="rounded bg-muted px-1 py-0.5">VITE_CLERK_PUBLISHABLE_KEY</code>{" "}
          in your <code className="rounded bg-muted px-1 py-0.5">.env</code> to enable Clerk sign-in.
          Meanwhile you're already signed in as a local dev user.
        </p>
      </div>
    );
  }
  return (
    <div className="flex h-full items-center justify-center p-8">
      <ClerkSignIn routing="path" path="/sign-in" />
    </div>
  );
}
