/**
 * The Teams guest display name.
 *
 * Teams' anonymous pre-join validates the name field: it accepts letters, numbers, spaces and
 * `- ' . _ @`, and anything else marks the field invalid and keeps "Join now" disabled. The
 * configured bot name is platform-neutral (Meet and Zoom accept "munshi.ai (recording)"), so the
 * Teams join adapts it here, at its own boundary: each run of rejected characters becomes a
 * " - " separator, so "munshi.ai (recording)" joins Teams as "munshi.ai - recording" and still
 * says it records.
 */

/** A run of characters Teams rejects in a guest name, with the spaces around it. */
const REJECTED_RUN = /\s*[^\p{L}\p{M}\p{N}\s\-'._@]+\s*/gu;

/** Used when nothing of the configured name survives Teams' rules. */
export const TEAMS_FALLBACK_GUEST_NAME = "munshi.ai - recording";

/** `name` rewritten to pass Teams' guest-name validation. */
export function teamsGuestName(name: string): string {
  const adapted = name
    .replace(REJECTED_RUN, " - ")
    .replace(/(\s*-\s*){2,}/g, " - ")
    .replace(/\s+/g, " ")
    .replace(/^[\s-]+|[\s-]+$/g, "");
  return adapted || TEAMS_FALLBACK_GUEST_NAME;
}

/** Machine-readable discriminator: the pre-join rendered but Teams kept "Join now" disabled. */
export const TEAMS_JOIN_DISABLED = "teams_join_disabled";

/**
 * The typed terminal for a pre-join whose "Join now" never enables. A bot standing there was
 * never sent to the lobby, so it must not be reported as waiting for admission. Thrown plain
 * (not an AdmissionError) for the same reason as `TeamsJoinRedirectError`: the orchestrator's
 * join catch carries the message into `last_error`.
 */
export class TeamsJoinDisabledError extends Error {
  readonly reasonCode = TEAMS_JOIN_DISABLED;
  constructor(teamsMessage: string | null) {
    super(
      `${TEAMS_JOIN_DISABLED}: Teams kept "Join now" disabled on the pre-join screen, so the bot ` +
        `never reached the lobby` + (teamsMessage ? ` (Teams said: ${teamsMessage})` : ""),
    );
    this.name = "TeamsJoinDisabledError";
  }
}
