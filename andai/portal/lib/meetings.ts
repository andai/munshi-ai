// Meetings through andai-core (same-origin /api/v1/meetings/* and /api/v1/search): list the
// meetings the caller may open, search them, send the bot, read one meeting, stop it, read its
// transcript and minutes, follow it live, share it inside the org, and delete it.
//
// `listMeetings` and `searchMeetings` return andai-core's pages as they are: andai-core decides
// which meetings a caller may see, so the portal never filters, merges or caches other people's
// meetings. Their cursors are opaque; the next page comes from passing one back unchanged.
//
// `subscribeLive` reads the transcript so far and opens the meeting's event stream (EventSource on
// /live) for what follows. When the stream fails — andai-core's `error` event, a dropped
// connection, or a stream that ends before the meeting does — it closes the EventSource (which
// would otherwise reconnect on its own) and polls the meeting and its transcript every 3 seconds
// instead, until the meeting ends. Segments are kept by id, so the same line from the stream, a
// poll, or both is shown once. The stream ends with the meeting's last status, and lines the
// platform delivers after it are not on it: the page reads the stored transcript once the meeting
// has ended (`mergeSegments`).
//
// The shapes are andai-core's; the portal adds no rules of its own.

import { ApiError, api } from "@/lib/api";

export type Platform = "google_meet" | "teams" | "zoom";
export type Language = "en" | "hi" | "hinglish";

export type MeetingStatus =
  | "scheduled"
  | "dispatching"
  | "awaiting_admission"
  | "recording"
  | "processing"
  | "done"
  | "failed"
  | "cancelled";

export type JoinFailureReason =
  | "admission_denied"
  | "admission_timeout"
  | "meeting_not_found"
  | "meeting_ended"
  | "auth_required"
  | "blocked_by_policy"
  | "removed_by_participant"
  | "ui_changed"
  | "capacity_exhausted"
  | "internal_error";

export type Consent = {
  bot_display_name: string;
  disclosure_channel: "chat" | "bot_name_only" | "tts";
  disclosure_detail: string | null;
  disclosure_posted_at: string | null;
  privacy_notice_url: string;
  removed_at: string | null;
};

/** Whether andai holds its own copy of a finished meeting's transcript (`stored`) yet. */
export type TranscriptState = "none" | "pending" | "stored" | "failed";

/** Someone the platform listed for the meeting: a calendar invitee or a speaker. */
export type Attendee = { name: string; source: "invite" | "speaker" };

/** What the caller may do with the meeting (andai-core decides; the page only shows controls). */
export type Permissions = { can_share: boolean; can_delete: boolean; can_stop: boolean };

/** One grant of read access: a person or a team of the org. */
export type Share = {
  id: string;
  grantee_type: "user" | "team";
  grantee_id: string;
  /** The person's or team's name; null when the team is gone. */
  name: string | null;
  granted_by: string;
  created_at: string;
};

export type Meeting = {
  id: string;
  team_id: string | null;
  team: TeamRef | null;
  requested_by: string;
  requested_by_name: string | null;
  permissions: Permissions;
  /** Who it is shared with, for callers who may share it; null for everyone else. */
  shared_with: Share[] | null;
  source: string;
  platform: Platform;
  native_meeting_id: string | null;
  join_url: string;
  status: MeetingStatus;
  failure_reason: JoinFailureReason | null;
  end_reason: string | null;
  admit_instructions: string | null;
  language: Language | null;
  bot_name: string;
  consent: Consent | null;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  updated_at: string;
  title: string | null;
  attendees: Attendee[];
  transcript_state: TranscriptState;
};

export type Script = "latn" | "deva" | "mixed" | "other";

export type Segment = {
  id: string;
  speaker: string;
  start: number | null;
  end: number | null;
  text: string;
  language: string | null;
  final: boolean;
  absolute_start: string | null;
  absolute_end: string | null;
  /** Stored segments only (andai's own copy): the line exactly as said. */
  text_raw?: string;
  /** Stored segments only: the line in Roman Hinglish. */
  text_canonical?: string;
  /** Stored segments only: the script of `text_raw`. */
  script?: Script;
};

/** One status as the live stream sends it; `at` is when andai-core saw the change. */
export type StatusUpdate = {
  status: MeetingStatus;
  failure_reason: JoinFailureReason | null;
  end_reason: string | null;
  admit_instructions: string | null;
  at: string;
};

export const TERMINAL: ReadonlySet<MeetingStatus> = new Set(["done", "failed", "cancelled"]);
export const POLL_INTERVAL_MS = 3_000;

export function isTerminal(status: MeetingStatus): boolean {
  return TERMINAL.has(status);
}

// ─── requests ──────────────────────────────────────────────────────────────────────────────────

/** Send the bot (201), or the meeting the bot is already in for this link (200). */
export function joinMeeting(body: { join_url: string; language: Language | null; team_id: string | null }): Promise<Meeting> {
  return api<Meeting>("/meetings/join", { method: "POST", body, redirect: false });
}

export function getMeeting(id: string): Promise<Meeting> {
  return api<Meeting>(`/meetings/${encodeURIComponent(id)}`);
}

export function stopMeeting(id: string): Promise<Meeting> {
  return api<Meeting>(`/meetings/${encodeURIComponent(id)}/stop`, { method: "POST", redirect: false });
}

export async function getTranscript(id: string): Promise<Segment[]> {
  const answer = await api<{ segments: Segment[] }>(`/meetings/${encodeURIComponent(id)}/transcript`);
  return answer.segments;
}

/**
 * Delete the meeting's audio, transcript and minutes (202). andai-core hides it from every read at
 * once and purges it within 15 minutes; 409 `meeting_live` while the bot is still in the meeting.
 */
export function deleteMeeting(id: string): Promise<{ id: string; purge_by: string }> {
  return api(`/meetings/${encodeURIComponent(id)}`, { method: "DELETE", redirect: false });
}

// ─── minutes ───────────────────────────────────────────────────────────────────────────────────

/** SPEC `MomDocument`; every `evidence` list names transcript segment ids. */
export type MomDecision = { text: string; evidence: string[] };
export type MomActionItem = {
  id: string;
  text: string;
  /** A name, or "unassigned". */
  owner: string;
  ownerUserId?: string;
  /** ISO date. */
  due?: string;
  evidence: string[];
};
export type MomTopic = { title: string; evidence: string[] };
export type MomRisk = { text: string; evidence: string[] };

export type MomDocument = {
  meetingId: string;
  version: number;
  templateId: string;
  outputLanguage: "en" | "hi" | "hinglish";
  summary: string;
  decisions: MomDecision[];
  actionItems: MomActionItem[];
  topics: MomTopic[];
  risks: MomRisk[];
  llmProfileId: string;
  model: string;
  generatedAt: string;
  editedBy?: string;
};

export const UNASSIGNED = "unassigned";

/** The latest minutes, or null when the meeting has none yet (404 `no_minutes`). */
export async function getMinutes(id: string): Promise<MomDocument | null> {
  try {
    return await api<MomDocument>(`/meetings/${encodeURIComponent(id)}/mom`, { redirect: false });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

// ─── sharing ───────────────────────────────────────────────────────────────────────────────────

/** The org's people (active members) and teams, names and ids only (`GET /org/directory`). */
export type Directory = {
  users: { id: string; name: string | null; team_ids: string[] }[];
  teams: { id: string; name: string }[];
};

export function getDirectory(): Promise<Directory> {
  return api<Directory>("/org/directory", { redirect: false });
}

export async function listShares(id: string): Promise<Share[]> {
  const answer = await api<{ shares: Share[] }>(`/meetings/${encodeURIComponent(id)}/shares`, { redirect: false });
  return answer.shares;
}

/** Share with a person or team of the org (the existing share when it is already shared). */
export function grantShare(id: string, grantee: { grantee_type: "user" | "team"; grantee_id: string }): Promise<Share> {
  return api<Share>(`/meetings/${encodeURIComponent(id)}/shares`, { method: "POST", body: grantee, redirect: false });
}

export async function revokeShare(id: string, shareId: string): Promise<void> {
  await api(`/meetings/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}`, { method: "DELETE", redirect: false });
}

// ─── the list and search ───────────────────────────────────────────────────────────────────────

export type Section = "live" | "upcoming" | "past";
export const SECTIONS: readonly Section[] = ["live", "upcoming", "past"];

/** `all`, `mine` (requested by me), `shared` (reaching me through a share) or `team:<id>`. */
export type ListFilter = "all" | "mine" | "shared" | `team:${string}`;

export type TeamRef = { id: string; name: string | null };

/** One row of the dashboard list (`GET /meetings`). */
export type MeetingItem = {
  id: string;
  title: string | null;
  platform: Platform;
  status: MeetingStatus;
  failure_reason: JoinFailureReason | null;
  end_reason: string | null;
  team: TeamRef | null;
  requested_by: string;
  requested_by_name: string;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  duration_s: number | null;
  shared: boolean;
  has_minutes: boolean;
};

export type MeetingPage = { items: MeetingItem[]; next_cursor: string | null };
export type MeetingSections = Partial<Record<Section, MeetingPage>>;

export const LIST_LIMIT = 25;

/**
 * One page of each section (no `section`), or of one section. With `cursor` (a `next_cursor` from
 * an earlier page) the answer is the page after it; the cursor carries its own section and filter.
 */
export async function listMeetings(
  options: { section?: Section; filter?: ListFilter; cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<MeetingSections> {
  const params = new URLSearchParams({ limit: String(options.limit ?? LIST_LIMIT) });
  if (options.section) params.set("section", options.section);
  if (options.cursor) params.set("cursor", options.cursor);
  else if (options.filter && options.filter !== "all") params.set("filter", options.filter);
  const answer = await api<{ sections: MeetingSections }>(`/meetings?${params}`, { signal: options.signal });
  return answer.sections;
}

/** `[start, end)` character offsets of the matched words in a hit's text. */
export type Mark = [number, number];

export type SearchHit =
  | { source: "title" | "minutes"; text: string; marks: Mark[] }
  | {
      source: "transcript";
      segment_id: string;
      speaker: string;
      start: number | null;
      /** The segment in Roman Hinglish (what search matched). */
      text: string;
      marks: Mark[];
      /** The segment as it was said (its original script). */
      text_raw: string;
      raw_marks: Mark[];
    };

export type SearchResult = {
  id: string;
  title: string | null;
  platform: Platform;
  status: MeetingStatus;
  team: TeamRef | null;
  started_at: string | null;
  created_at: string;
  hits: SearchHit[];
};

export type SearchPage = { query: string; total: number; results: SearchResult[]; next_cursor: string | null };

/** Meetings the caller may open whose title, transcript or minutes match `q`, best first. */
export function searchMeetings(q: string, options: { cursor?: string | null; signal?: AbortSignal } = {}): Promise<SearchPage> {
  const params = new URLSearchParams({ q });
  if (options.cursor) params.set("cursor", options.cursor);
  return api<SearchPage>(`/search?${params}`, { signal: options.signal });
}

// ─── segments ──────────────────────────────────────────────────────────────────────────────────

function byStart(a: Segment, b: Segment): number {
  const left = a.start ?? Number.POSITIVE_INFINITY;
  const right = b.start ?? Number.POSITIVE_INFINITY;
  if (left !== right) return left - right;
  return (a.absolute_start ?? "").localeCompare(b.absolute_start ?? "") || a.id.localeCompare(b.id);
}

/** Segments kept by id: a later copy of a segment replaces the earlier one. */
export class SegmentStore {
  private readonly segments = new Map<string, Segment>();

  put(segment: Segment): void {
    this.segments.set(segment.id, segment);
  }

  putAll(segments: Segment[]): void {
    for (const segment of segments) this.put(segment);
  }

  remove(ids: string[]): void {
    for (const id of ids) this.segments.delete(id);
  }

  list(): Segment[] {
    return [...this.segments.values()].sort(byStart);
  }
}

/** `current` with `more` merged in by id, in order. */
export function mergeSegments(current: Segment[], more: Segment[]): Segment[] {
  const store = new SegmentStore();
  store.putAll(current);
  store.putAll(more);
  return store.list();
}

// ─── live ──────────────────────────────────────────────────────────────────────────────────────

export type LiveMode = "live" | "polling" | "ended";

export type LiveHandlers = {
  /** A status from the stream, or a changed one from a poll. */
  onStatus: (update: StatusUpdate) => void;
  /** The whole transcript so far, in order, after any change. */
  onSegments: (segments: Segment[]) => void;
  /** The meeting record, from each poll. */
  onMeeting?: (meeting: Meeting) => void;
  /** How updates arrive now. */
  onMode?: (mode: LiveMode) => void;
};

function parse<T>(event: MessageEvent): T | null {
  try {
    return JSON.parse(String(event.data)) as T;
  } catch {
    return null;
  }
}

/**
 * Follow one meeting: the live stream first, polling when it fails. Returns the function that
 * stops following (closes the stream, ends polling).
 */
export function subscribeLive(id: string, handlers: LiveHandlers): () => void {
  const store = new SegmentStore();
  let stopped = false;
  let mode: LiveMode = "live";
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastStatus: string | null = null;

  const setMode = (next: LiveMode) => {
    if (mode === next) return;
    mode = next;
    handlers.onMode?.(next);
  };

  const closeStream = () => {
    if (source) {
      source.close();
      source = null;
    }
  };

  const finish = () => {
    closeStream();
    if (timer) clearTimeout(timer);
    timer = null;
    setMode("ended");
  };

  const status = (update: StatusUpdate) => {
    const key = `${update.status}|${update.failure_reason ?? ""}|${update.end_reason ?? ""}`;
    if (key !== lastStatus) {
      lastStatus = key;
      handlers.onStatus(update);
    }
    if (isTerminal(update.status)) finish();
  };

  const poll = async () => {
    timer = null;
    if (stopped) return;
    try {
      const [meeting, segments] = await Promise.all([getMeeting(id), getTranscript(id)]);
      if (stopped) return;
      handlers.onMeeting?.(meeting);
      store.putAll(segments);
      handlers.onSegments(store.list());
      status({
        status: meeting.status,
        failure_reason: meeting.failure_reason,
        end_reason: meeting.end_reason,
        admit_instructions: meeting.admit_instructions,
        at: meeting.updated_at,
      });
      if (isTerminal(meeting.status)) return;
    } catch {
      // A failed poll is retried on the next tick; a 401 has already left the page (lib/api.ts).
    }
    if (!stopped) timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
  };

  const fallBack = () => {
    if (stopped || mode !== "live") return;
    closeStream();
    setMode("polling");
    void poll();
  };

  source = new EventSource(`/api/v1/meetings/${encodeURIComponent(id)}/live`);
  source.addEventListener("status", (event) => {
    const update = parse<StatusUpdate>(event as MessageEvent);
    if (update && !stopped) status(update);
  });
  source.addEventListener("segment", (event) => {
    const segment = parse<Segment>(event as MessageEvent);
    if (!segment || stopped) return;
    store.put(segment);
    handlers.onSegments(store.list());
  });
  source.addEventListener("retract", (event) => {
    const retracted = parse<{ ids: string[] }>(event as MessageEvent);
    if (!retracted || stopped) return;
    store.remove(retracted.ids);
    handlers.onSegments(store.list());
  });
  // Both andai-core's own `error` event and a failed or dropped connection arrive here.
  source.addEventListener("error", () => fallBack());
  // The lines spoken before the stream opened (the stream carries only what comes next).
  getTranscript(id)
    .then((segments) => {
      if (stopped) return;
      store.putAll(segments);
      handlers.onSegments(store.list());
    })
    .catch(() => {
      // the stream or the polls fill it in
    });

  return () => {
    stopped = true;
    closeStream();
    if (timer) clearTimeout(timer);
    timer = null;
  };
}
