/**
 * JWT auth provider + `useIdentity()` hook.
 *
 * Mirrors the Clerk block's surface (`useIdentity()`, `getToken()`,
 * `signOut()`) so app components are auth-provider-agnostic.
 *
 * Tokens live in `sessionStorage` — refreshed on every login. We chose
 * sessionStorage over localStorage to limit XSS blast radius (token
 * dies with the tab) at the cost of forcing re-login per browser
 * session. Generated apps that need persistence can swap to
 * localStorage with a one-line edit.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

const STORAGE_KEY = "alloy.jwt";

interface JwtIdentity {
  isLoaded: boolean;
  isSignedIn: boolean;
  userId: string | null;
  email: string | null;
  getToken: () => Promise<string | null>;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<void>;
  signOut: () => void;
}

const JwtContext = createContext<JwtIdentity | null>(null);

interface MePayload {
  id: string;
  email: string;
  is_active?: boolean;
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";

async function fetchMe(token: string): Promise<MePayload | null> {
  try {
    const res = await fetch(`${API_BASE}/api/v1/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return null;
    return (await res.json()) as MePayload;
  } catch {
    return null;
  }
}

export function JwtAuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() =>
    typeof sessionStorage !== "undefined" ? sessionStorage.getItem(STORAGE_KEY) : null,
  );
  const [me, setMe] = useState<MePayload | null>(null);
  const [isLoaded, setIsLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!token) {
      setMe(null);
      setIsLoaded(true);
      return;
    }
    fetchMe(token).then((payload) => {
      if (cancelled) return;
      setMe(payload);
      setIsLoaded(true);
      if (payload === null) {
        sessionStorage.removeItem(STORAGE_KEY);
        setToken(null);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const signIn = useCallback(async (email: string, password: string) => {
    const body = new URLSearchParams();
    body.set("username", email);
    body.set("password", password);
    const res = await fetch(`${API_BASE}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => null)) as { detail?: string } | null;
      throw new Error(detail?.detail ?? `Login failed (${res.status})`);
    }
    const json = (await res.json()) as { access_token: string };
    sessionStorage.setItem(STORAGE_KEY, json.access_token);
    setToken(json.access_token);
  }, []);

  const signUp = useCallback(async (email: string, password: string) => {
    const res = await fetch(`${API_BASE}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => null)) as { detail?: string } | null;
      throw new Error(detail?.detail ?? `Signup failed (${res.status})`);
    }
    const json = (await res.json()) as { access_token: string };
    sessionStorage.setItem(STORAGE_KEY, json.access_token);
    setToken(json.access_token);
  }, []);

  const signOut = useCallback(() => {
    sessionStorage.removeItem(STORAGE_KEY);
    setToken(null);
    setMe(null);
  }, []);

  const getToken = useCallback(async () => token, [token]);

  const value: JwtIdentity = useMemo(
    () => ({
      isLoaded,
      isSignedIn: Boolean(me),
      userId: me?.id ?? null,
      email: me?.email ?? null,
      getToken,
      signIn,
      signUp,
      signOut,
    }),
    [isLoaded, me, getToken, signIn, signUp, signOut],
  );

  return <JwtContext.Provider value={value}>{children}</JwtContext.Provider>;
}

export function useIdentity(): JwtIdentity {
  const ctx = useContext(JwtContext);
  if (ctx === null) {
    throw new Error("useIdentity() must be called inside <JwtAuthProvider>");
  }
  return ctx;
}
