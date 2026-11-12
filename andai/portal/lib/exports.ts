// Copies of a person's data (OPS-02, DPDP access request), through andai-core: the signed-in
// person's own (POST/GET /api/v1/me/exports), a member's asked for by an owner or admin
// (POST /api/v1/org/users/{id}/exports, GET /api/v1/org/exports), and the download, a plain link
// the browser follows (GET /api/v1/exports/{id}/download, streamed through the portal's proxy).
// andai-core decides who may ask and who may download; `canDownload` is its answer per copy.

import { api } from "@/lib/api";

/** Waiting for andai-worker, being made, ready to download, could not be made, or past its time. */
export type ExportState = "pending" | "running" | "ready" | "failed" | "expired";

export type DataExport = {
  id: string;
  subjectId: string;
  /** The member's display name, in the organisation's list only. */
  subjectName?: string;
  /** `self`: the person asked; `org_admin`: an owner or admin asked for them. */
  source: "self" | "org_admin";
  state: ExportState;
  /** Why it could not be made: `too_large`, `not_a_member` or `internal_error`. */
  error: string | null;
  createdAt: string;
  readyAt: string | null;
  /** When the copy is deleted; set once it is ready. */
  expiresAt: string | null;
  /** The size of every file of the copy, recordings included. */
  bytes: number | null;
  canDownload: boolean;
};

export function isOpen(item: DataExport): boolean {
  return item.state === "pending" || item.state === "running";
}

/** The signed-in person's copies in this organisation, newest first. */
export function listMyExports(options: { signal?: AbortSignal } = {}): Promise<{ items: DataExport[] }> {
  return api<{ items: DataExport[] }>("/me/exports", { signal: options.signal });
}

/** Ask for a copy of one's own data; 429 while one is being made or after three in a day. */
export function requestMyExport(): Promise<DataExport> {
  return api<DataExport>("/me/exports", { method: "POST", redirect: false });
}

export type ExportPage = { items: DataExport[]; next: string | null };

/** The copies owners and admins asked for, newest first, 50 per page. */
export function listOrgExports(options: { cursor?: string | null; signal?: AbortSignal } = {}): Promise<ExportPage> {
  const query = options.cursor ? `?${new URLSearchParams({ cursor: options.cursor })}` : "";
  return api<ExportPage>(`/org/exports${query}`, { signal: options.signal });
}

/** Ask for a copy of a member's data (owners and admins). */
export function requestMemberExport(userId: string): Promise<DataExport> {
  return api<DataExport>(`/org/users/${encodeURIComponent(userId)}/exports`, { method: "POST", redirect: false });
}

/** Where the browser downloads a ready copy from. */
export function downloadHref(id: string): string {
  return `/api/v1/exports/${encodeURIComponent(id)}/download`;
}

export const EXPORT_POLL_MS = 3000;

const ERRORS: Record<string, string> = {
  too_large: "Too much data for one copy; ask your munshi.ai operator.",
  not_a_member: "They were no longer a member when the copy was made.",
  internal_error: "The copy could not be made; ask for a new one.",
};

export function failureText(error: string | null): string {
  return (error && ERRORS[error]) || ERRORS.internal_error;
}

export function sizeText(bytes: number | null): string | null {
  if (bytes === null) return null;
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function dayText(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function timeText(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}
