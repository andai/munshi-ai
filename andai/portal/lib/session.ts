"use client";

// The signed-in account as andai-core describes it (GET /api/v1/me), shared by the app shell and
// its pages. The shell loads it once; a page that changes the account calls `refresh()`.

import { createContext, createElement, useContext, type ReactNode } from "react";

import { api } from "@/lib/api";

export type OrgRole = "owner" | "admin" | "member";

export type Me = {
  user: { id: string; email: string; display_name: string | null };
  org: { id: string; name: string; slug: string };
  role: OrgRole;
  stage: string;
  mfa_enabled: boolean;
};

export type Session = {
  me: Me;
  /** Whether the account leads at least one team (from GET /api/v1/teams). */
  leadsATeam: boolean;
  refresh: () => Promise<void>;
};

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ value, children }: { value: Session; children: ReactNode }) {
  return createElement(SessionContext.Provider, { value }, children);
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession is used outside the app shell");
  return session;
}

export function isAdmin(role: OrgRole): boolean {
  return role === "owner" || role === "admin";
}

export function displayName(user: Me["user"]): string {
  return user.display_name?.trim() || user.email;
}

/** End the session on the server, then leave for the sign-in page. */
export async function signOut(): Promise<void> {
  try {
    await api("/auth/logout", { method: "POST", redirect: false });
  } finally {
    window.location.assign("/sign-in");
  }
}
