// Words for andai-core's meeting values. The portal decides nothing here: every map is keyed by an
// andai enum, and a value this file does not know is shown in a neutral form rather than guessed.

import type { JoinFailureReason, Language, MeetingStatus, Platform } from "@/lib/meetings";

export const PLATFORM_NAME: Record<Platform, string> = {
  google_meet: "Google Meet",
  teams: "Microsoft Teams",
  zoom: "Zoom",
};

export const LANGUAGE_NAME: Record<Language, string> = {
  en: "English",
  hi: "Hindi",
  hinglish: "Hinglish",
};

export type Tone = "neutral" | "waiting" | "live" | "success" | "danger";

export const STATUS: Record<MeetingStatus, { label: string; tone: Tone }> = {
  scheduled: { label: "Scheduled", tone: "neutral" },
  dispatching: { label: "Joining", tone: "waiting" },
  awaiting_admission: { label: "Waiting to be let in", tone: "waiting" },
  recording: { label: "Recording", tone: "live" },
  processing: { label: "Finishing up", tone: "waiting" },
  done: { label: "Done", tone: "success" },
  failed: { label: "Could not join", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export function statusLabel(status: string): string {
  return STATUS[status as MeetingStatus]?.label ?? status;
}

/** Why the bot could not join or stay, per JoinFailureReason. */
export const FAILURE: Record<JoinFailureReason, { title: string; text: string }> = {
  admission_denied: {
    title: "The bot was not let in",
    text: "Someone in the meeting declined the bot's request to join.",
  },
  admission_timeout: {
    title: "Nobody let the bot in",
    text: "The bot waited in the lobby, but no one admitted it before the waiting time ran out.",
  },
  meeting_not_found: {
    title: "Meeting not found",
    text: "The link did not lead to a meeting. Check the link and send the bot again.",
  },
  meeting_ended: {
    title: "The meeting had ended",
    text: "The meeting was already over when the bot arrived.",
  },
  auth_required: {
    title: "The meeting needs a signed-in account",
    text: "This meeting admits only signed-in people, so the bot could not join as a guest.",
  },
  blocked_by_policy: {
    title: "Blocked by a meeting policy",
    text: "The meeting platform or the organiser's settings do not allow the bot to join.",
  },
  removed_by_participant: {
    title: "Removed from the meeting",
    text: "A participant removed the bot from the meeting.",
  },
  ui_changed: {
    title: "The bot could not get in",
    text: "The bot could not get through the platform's join screen.",
  },
  capacity_exhausted: {
    title: "Too many bots at once",
    text: "Your organisation is already running as many bots as it can. Try again when a meeting ends.",
  },
  internal_error: {
    title: "Something went wrong",
    text: "The bot stopped because of a problem on our side. Try sending it again.",
  },
};

export function failureCopy(reason: string | null): { title: string; text: string } {
  return FAILURE[reason as JoinFailureReason] ?? FAILURE.internal_error;
}

/** How a finished meeting ended, per the platform's completion reason andai-core reports. */
const END: Record<string, string> = {
  stopped: "The bot was stopped and left the meeting.",
  left_alone: "The bot left after everyone else had gone.",
  startup_alone: "The bot left because no one else joined.",
  evicted: "A participant removed the bot from the meeting.",
  max_bot_time_exceeded: "The bot left when it reached the longest time it may stay.",
  stopped_with_no_audio: "The bot left because it heard no audio.",
  stopped_before_admission: "The bot was stopped before anyone let it in.",
};

export function endText(reason: string | null): string | null {
  return reason ? (END[reason] ?? null) : null;
}

/** The host part of a meeting link (never its query, where a passcode can sit). */
export function linkHost(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function clockTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : timeFormat.format(date);
}

/** Seconds from the start of the meeting as m:ss (h:mm:ss past an hour). */
export function offset(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "--:--";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${String(m).padStart(2, "0")}:${s}`;
}
