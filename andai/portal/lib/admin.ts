// Organisation settings and records for owners and admins, through andai-core (same-origin
// /api/v1/org/*): the bot's auto-join setting for the organisation and per team
// (GET/PUT /org/bot-settings), and the notetaker mail andai-intake quarantined for this organisation
// (GET /org/intake/quarantine). andai-core decides who may read or change them (members and leads
// get 403) and which rows a caller sees; the portal shows its answers as they are.

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
