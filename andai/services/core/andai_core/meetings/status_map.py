"""The platform's meeting record → andai's meeting status, failure reason and admission facts.

A pure table over the platform's `status`, `completion_reason`, `failure_stage`, `start_time` and
`data.join_evidence` (api.v1 `MeetingResponse`):

| platform status                          | andai status                                   |
|------------------------------------------|------------------------------------------------|
| requested, joining                       | dispatching                                    |
| awaiting_admission, needs_human_help,    | awaiting_admission                             |
|   needs_help                             |                                                |
| active                                   | recording                                      |
| stopping                                 | processing                                     |
| completed                                | done (cancelled if stopped before admission)   |
| failed                                   | failed with a `JoinFailureReason`              |

Phase 4 has no post-meeting pipeline, so `processing` ends in `done` as soon as the platform
reports `completed`. A `completed` meeting keeps the platform's reason as `end_reason`
(`left_alone`, `evicted`, `stopped`, ...). A status or completion reason this table does not know
maps to `failed` / `internal_error` and is named in `unknown`, so the caller can log the raw value.

Failure reasons, strongest evidence first: a platform-policy refusal named in the join evidence or
the bot's last error (Teams' disabled "Join now" `teams_join_disabled`, Zoom's anti-bot wall
`zoom_requires_rtms`, a captcha, or attribution `exogenous_platform`) is `blocked_by_policy`; then
the completion reason; then the join evidence's own reason.
"""

from __future__ import annotations

from dataclasses import dataclass

DISPATCHING = frozenset({"requested", "joining"})
AWAITING = frozenset({"awaiting_admission", "needs_human_help", "needs_help"})

# Completion reasons that only happen after the bot was admitted.
ADMITTED_ENDS = frozenset(
    {
        "stopped",
        "left_alone",
        "startup_alone",
        "evicted",
        "max_bot_time_exceeded",
        "stopped_with_no_audio",
    }
)
JOIN_FAILURES = frozenset(
    {
        "awaiting_admission_timeout",
        "awaiting_admission_rejected",
        "join_failure",
        "auth_session_missing",
        "validation_error",
    }
)
STOPPED_BEFORE_ADMISSION = "stopped_before_admission"

POLICY_MARKERS = (
    "teams_join_disabled",
    "zoom_requires_rtms",
    "blocks automated browser joins",
    "automated bots",
    "anti-bot",
    "recaptcha",
    "captcha",
    "bot detection",
    "bot-detection",
    "unusual traffic",
)
ENDED_MARKERS = ("meeting has ended", "meeting has been ended", "meeting ended", "this meeting has ended")
NOT_FOUND_MARKERS = ("invalid meeting", "meeting not found", "does not exist", "not a valid meeting")
AUTH_MARKERS = ("teams_auth_redirect", "sign in to join", "requires sign-in", "authentication required")
INTERNAL_MARKERS = ("control_plane_unreachable",)


@dataclass(frozen=True)
class Mapped:
    status: str
    failure_reason: str | None = None
    end_reason: str | None = None
    # The bot was let into the meeting at some point (a consent record is due).
    admitted: bool = False
    # A participant removed the bot.
    removed: bool = False
    # The raw platform value this table did not know, for the caller to log.
    unknown: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in ("done", "failed", "cancelled")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _evidence(meeting: dict) -> dict:
    data = meeting.get("data")
    evidence = data.get("join_evidence") if isinstance(data, dict) else None
    return evidence if isinstance(evidence, dict) else {}


def _last_error(meeting: dict) -> str | None:
    """The bot's own failure message: `data.reason` (kept in the list view) and
    `data.last_error.reason` / `error_details` (detail read only)."""
    data = meeting.get("data")
    if not isinstance(data, dict):
        return None
    parts = [_text(data.get("reason"))]
    last = data.get("last_error")
    if isinstance(last, dict):
        parts += [_text(last.get("reason")), _text(last.get("error_details"))]
    text = " ".join(p for p in parts if p)
    return text or None


def _field(meeting: dict, name: str) -> str | None:
    """A hoisted typed field, falling back to the same key in `data`."""
    value = _text(meeting.get(name))
    if value is None and isinstance(meeting.get("data"), dict):
        value = _text(meeting["data"].get(name))
    return value


def _has(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def failure_reason(
    completion_reason: str | None,
    failure_stage: str | None,
    evidence: dict,
    last_error: str | None = None,
) -> str:
    """The `JoinFailureReason` of a failed platform meeting. `last_error` is the bot's own message
    (`data.reason`, `data.last_error`), searched with the evidence's detail."""
    detail = " ".join(filter(None, (_text(evidence.get("detail")), last_error))).lower()
    evidence_reason = _text(evidence.get("reason"))
    if evidence.get("attribution") == "exogenous_platform" or _has(detail, POLICY_MARKERS):
        return "blocked_by_policy"
    if _has(detail, ENDED_MARKERS):
        return "meeting_ended"
    if _has(detail, INTERNAL_MARKERS):
        return "internal_error"
    reason = completion_reason or evidence_reason
    if reason == "awaiting_admission_rejected":
        return "admission_denied"
    if reason == "awaiting_admission_timeout":
        return "admission_timeout"
    if reason == "auth_session_missing" or _has(detail, AUTH_MARKERS):
        return "auth_required"
    if reason == "join_failure" and failure_stage == "active":
        return "internal_error"  # admitted, then the capture failed to start
    if evidence_reason == "awaiting_admission_rejected":
        return "admission_denied"
    if evidence_reason == "awaiting_admission_timeout":
        return "admission_timeout"
    if evidence_reason == "never_reached_lobby":
        return "ui_changed"
    if _has(detail, NOT_FOUND_MARKERS) or evidence_reason == "navigation_failure":
        return "meeting_not_found"
    return "internal_error"


def map_platform(meeting: dict) -> Mapped:
    """Map one platform meeting record (api.v1 `MeetingResponse`)."""
    status = _text(meeting.get("status"))
    reason = _field(meeting, "completion_reason")
    stage = _field(meeting, "failure_stage")
    evidence = _evidence(meeting)
    last_error = _last_error(meeting)
    started = _text(meeting.get("start_time")) is not None

    if status in DISPATCHING:
        return Mapped("dispatching")
    if status in AWAITING:
        return Mapped("awaiting_admission")
    if status == "active":
        return Mapped("recording", admitted=True)
    if status == "stopping":
        return Mapped("processing", admitted=started)
    if status == "completed":
        if reason == STOPPED_BEFORE_ADMISSION or evidence.get("reason") == "stopped_while_joining":
            return Mapped("cancelled", end_reason=reason, admitted=started)
        if reason in ADMITTED_ENDS:
            return Mapped("done", end_reason=reason, admitted=True, removed=reason == "evicted")
        if reason in JOIN_FAILURES:
            return Mapped(
                "failed", failure_reason(reason, stage, evidence, last_error), end_reason=reason,
                admitted=started,
            )
        if reason is None:
            return Mapped("done", admitted=started)
        return Mapped(
            "failed", "internal_error", end_reason=reason, admitted=started,
            unknown=f"completion_reason={reason}",
        )
    if status == "failed":
        if reason == STOPPED_BEFORE_ADMISSION or evidence.get("reason") == "stopped_while_joining":
            return Mapped("cancelled", end_reason=reason, admitted=started)
        admitted = started or stage == "active"
        if reason == "evicted":
            return Mapped("done", end_reason=reason, admitted=True, removed=True)
        unknown = None
        if reason is not None and reason not in JOIN_FAILURES and reason not in ADMITTED_ENDS:
            unknown = f"completion_reason={reason}"
        return Mapped(
            "failed", failure_reason(reason, stage, evidence, last_error), end_reason=reason,
            admitted=admitted, unknown=unknown,
        )
    return Mapped("failed", "internal_error", admitted=started, unknown=f"status={status}")


ADMIT_INSTRUCTIONS = {
    "google_meet": (
        'The bot is waiting to be let in. Someone hosting the Google Meet call admits "{bot}" from '
        "the people waiting to join. If Meet lists the bot under \"With potential risks\", open "
        "the ⋮ menu next to it and choose Admit."
    ),
    "teams": (
        'The bot is in the Teams lobby. An organiser or presenter admits "{bot}" from the lobby '
        "list in the meeting's People pane. If your organisation's Teams policy keeps external "
        "participants or bots in the lobby, your Teams administrator can follow the munshi.ai "
        "Teams tenant runbook to allow it."
    ),
    "zoom": (
        'The bot is in the Zoom waiting room. The host or a co-host admits "{bot}" from '
        "Participants → Waiting room → Admit."
    ),
}


def admit_instructions(platform: str, bot_name: str) -> str | None:
    template = ADMIT_INSTRUCTIONS.get(platform)
    return None if template is None else template.format(bot=bot_name)
