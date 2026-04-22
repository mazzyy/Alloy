import { Route, Routes, Navigate } from "react-router-dom";
import { Dashboard } from "@/pages/Dashboard";
import { SignIn } from "@/pages/SignIn";
import { useIdentity } from "@/auth/identity";

export default function App() {
  const { isLoaded, isSignedIn } = useIdentity();

  if (!isLoaded) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-muted-foreground">Loading Alloy…</p>
      </div>
    );
  }

  return (
    <Routes>
      <Route path="/sign-in/*" element={<SignIn />} />
      <Route
        path="/"
        element={isSignedIn ? <Dashboard /> : <Navigate to="/sign-in" replace />}
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
