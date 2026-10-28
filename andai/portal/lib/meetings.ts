// Meetings through andai-core (same-origin /api/v1/meetings/*): send the bot, read one meeting,
// stop it, read its transcript, and follow it live.
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

import { api } from "@/lib/api";

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

export type Meeting = {
  id: string;
  team_id: string | null;
  requested_by: string;
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
};

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
