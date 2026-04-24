import { Route, Routes, Navigate } from "react-router-dom";
import { Dashboard } from "@/pages/Dashboard";
import { SignIn } from "@/pages/SignIn";
import { Build } from "@/pages/Build";
import { useIdentity } from "@/auth/identity";
import type { ReactNode } from "react";

export default function App() {
  const { isLoaded, isSignedIn } = useIdentity();

  if (!isLoaded) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-muted-foreground">Loading Alloy…</p>
      </div>
    );
  }

  const guard = (el: ReactNode) => (isSignedIn ? el : <Navigate to="/sign-in" replace />);

  return (
    <Routes>
      <Route path="/sign-in/*" element={<SignIn />} />
      <Route path="/" element={guard(<Dashboard />)} />
      <Route path="/build" element={guard(<Build />)} />
      <Route path="/build/:projectId" element={guard(<Build />)} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
