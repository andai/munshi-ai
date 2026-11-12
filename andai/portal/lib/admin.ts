// Organisation settings and records for owners and admins, through andai-core (same-origin
// /api/v1/org/*): the bot's auto-join setting for the organisation and per team
// (GET/PUT /org/bot-settings), the notetaker mail andai-intake quarantined for this organisation
// (GET /org/intake/quarantine), the organisation's audit trail (GET /org/audit and
// /org/audit/actions) and erasing a member's data (POST /org/users/{id}/erasure, GET /org/erasures).
// andai-core decides who may read or change them (members and leads get 403) and which rows a caller
// sees; the portal shows its answers as they are.

import { api } from "@/lib/api";

/**
 * When mail to the notetaker schedules a meeting: invites sent to the notetaker
 * (`only_when_invited`, the default), also copies a mailbox rule forwards (`all_with_link`), or never
 * (`off`; changes and cancellations of meetings already scheduled still apply).
 */
export type AutoJoin = "only_when_invited" | "all_with_link" | "off";

export const AUTO_JOIN_VALUES: readonly AutoJoin[] = ["only_when_invited", "all_with_link", "off"];

/** The organisation's value and every team's override (`null`: the team follows the organisation). */
export type BotSettings = {
  autoJoin: AutoJoin;
  teams: { teamId: string; autoJoin: AutoJoin | null }[];
};

export function getBotSettings(): Promise<BotSettings> {
  return api<BotSettings>("/org/bot-settings");
}

/** Save the organisation's value and the listed teams' overrides; answers the new settings. */
export function saveBotSettings(settings: BotSettings): Promise<BotSettings> {
  return api<BotSettings>("/org/bot-settings", { method: "PUT", body: settings, redirect: false });
}

export type QuarantineReason = "sender_unverified" | "unknown_user" | "no_meeting_link" | "parse_error" | "rate_limited";

/** One quarantined message: never its body, envelope or delivering address. */
export type QuarantineItem = {
  receivedAt: string;
  /** The From address andai-intake read; null when the mail named none, or several. */
  sender: string | null;
  /** Trimmed to 120 characters by andai-core. */
  subject: string | null;
  reason: QuarantineReason;
  senderAuth: "dmarc_pass" | "arc_trusted" | "fail";
  /** One sentence about the sender check; null when the mail has no check summary. */
  check: string | null;
};

export type QuarantinePage = { items: QuarantineItem[]; next_cursor: string | null };

/** Newest first, 25 per page; with `cursor` (a `next_cursor`), the page after it. */
export function listQuarantine(options: { cursor?: string | null; signal?: AbortSignal } = {}): Promise<QuarantinePage> {
  const params = new URLSearchParams();
  if (options.cursor) params.set("cursor", options.cursor);
  const query = params.size ? `?${params}` : "";
  return api<QuarantinePage>(`/org/intake/quarantine${query}`, { signal: options.signal });
}

/** One audited action andai-core knows, for the filter and the row labels. */
export type AuditAction = { key: string; category: string; label: string };

export function listAuditActions(options: { signal?: AbortSignal } = {}): Promise<AuditAction[]> {
  return api<AuditAction[]>("/org/audit/actions", { signal: options.signal });
}

/** One audit event. `actor.id` is null for the system and for a failed sign-in; `name` is null for a
 * user who no longer exists. `target.label` is a member's name, a team's name or a meeting's title
 * when andai-core may show one, else null. */
export type AuditEvent = {
  id: string;
  at: string;
  actor: { id: string | null; name: string | null };
  action: string;
  category: string | null;
  target: { type: string; id: string; label: string | null };
  ip: string | null;
};

export type AuditPage = { items: AuditEvent[]; next: string | null };

/** Whole UTC days, both inclusive (`YYYY-MM-DD`; andai-core's default is the last 30 days), and one
 * action key or none. */
export type AuditFilters = { from?: string; to?: string; action?: string };

/** Newest first, 50 per page; with `cursor` (a `next`), the page after it under the same filters. */
export function listAudit(
  filters: AuditFilters,
  options: { cursor?: string | null; signal?: AbortSignal } = {},
): Promise<AuditPage> {
  const params = new URLSearchParams();
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (filters.action) params.set("action", filters.action);
  if (options.cursor) params.set("cursor", options.cursor);
  const query = params.size ? `?${params}` : "";
  return api<AuditPage>(`/org/audit${query}`, { signal: options.signal });
}

/** Where an erasure is: waiting for andai-worker, being worked, finished, or stopped by an error the
 * operator has to look at. */
export type ErasureState = "pending" | "running" | "done" | "failed";

/** One erasure of a person's data in this organisation: ids and states only, never a name or an
 * address. */
export type ErasureRequest = {
  id: string;
  subjectId: string;
  state: ErasureState;
  step: string;
  createdAt: string;
  finishedAt: string | null;
  error: string | null;
};

export type ErasurePage = { items: ErasureRequest[]; next: string | null };

/** Newest first, 50 per page; with `cursor` (a `next`), the page after it. */
export function listErasures(options: { cursor?: string | null; signal?: AbortSignal } = {}): Promise<ErasurePage> {
  const query = options.cursor ? `?${new URLSearchParams({ cursor: options.cursor })}` : "";
  return api<ErasurePage>(`/org/erasures${query}`, { signal: options.signal });
}

/** Ask andai-core to erase a member's data in this organisation. `confirm` is their email address as
 * the admin typed it. Their access ends at once; the rest happens within 24 hours (`eraseBy`). */
export function requestErasure(
  userId: string,
  confirm: string,
): Promise<{ id: string; state: ErasureState; eraseBy: string }> {
  return api(`/org/users/${encodeURIComponent(userId)}/erasure`, {
    method: "POST",
    body: { confirm },
    redirect: false,
  });
}
