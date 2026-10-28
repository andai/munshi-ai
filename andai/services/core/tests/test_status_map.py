"""The platform → andai status table, over every status and completion reason the sealed api.v1
contract names, plus the platform-specific refusals (Teams' disabled "Join now", Zoom's wall)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from andai_core.db.models import JOIN_FAILURE_REASONS, MEETING_STATUSES
from andai_core.meetings.status_map import Mapped, admit_instructions, map_platform

API_V1 = (
    Path(__file__).resolve().parents[4] / "core" / "gateway" / "contracts" / "api.v1"
    / "api.schema.json"
)
SCHEMAS = json.loads(API_V1.read_text())["components"]["schemas"]
PLATFORM_STATUSES = tuple(SCHEMAS["MeetingStatus"]["enum"])
COMPLETION_REASONS = tuple(SCHEMAS["MeetingCompletionReason"]["enum"])
STARTED = "2026-10-28T10:00:00Z"


def meeting(status: str, reason: str | None = None, **extra) -> dict:
    data = extra.pop("data", {})
    return {
        "id": 1,
        "status": status,
        "completion_reason": reason,
        "start_time": extra.pop("start_time", None),
        "data": data,
        **extra,
    }


def test_the_contract_enumerations_are_the_ones_mapped() -> None:
    assert set(PLATFORM_STATUSES) == {
        "requested", "joining", "awaiting_admission", "active", "needs_human_help", "stopping",
        "completed", "failed",
    }
    assert len(COMPLETION_REASONS) == 12


@pytest.mark.parametrize("status", [*PLATFORM_STATUSES, "needs_help"])
def test_every_platform_status_maps_without_an_unknown(status: str) -> None:
    mapped = map_platform(meeting(status))
    assert mapped.unknown is None
    assert mapped.status in MEETING_STATUSES
    assert (mapped.status == "failed") == (mapped.failure_reason is not None)


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("reason", COMPLETION_REASONS)
def test_every_completion_reason_maps_without_an_unknown(status: str, reason: str) -> None:
    mapped = map_platform(meeting(status, reason))
    assert mapped.unknown is None, mapped
    assert mapped.terminal
    if mapped.status == "failed":
        assert mapped.failure_reason in JOIN_FAILURE_REASONS
    else:
        assert mapped.failure_reason is None


TABLE = [
    # (platform record, expected)
    (meeting("requested"), Mapped("dispatching")),
    (meeting("joining"), Mapped("dispatching")),
    (meeting("awaiting_admission"), Mapped("awaiting_admission")),
    (meeting("needs_human_help"), Mapped("awaiting_admission")),
    (meeting("needs_help"), Mapped("awaiting_admission")),
    (meeting("active", start_time=STARTED), Mapped("recording", admitted=True)),
    (meeting("stopping", start_time=STARTED), Mapped("processing", admitted=True)),
    (meeting("stopping"), Mapped("processing")),
    (
        meeting("completed", "stopped", start_time=STARTED),
        Mapped("done", end_reason="stopped", admitted=True),
    ),
    (
        meeting("completed", "left_alone"),
        Mapped("done", end_reason="left_alone", admitted=True),
    ),
    (
        meeting("completed", "startup_alone"),
        Mapped("done", end_reason="startup_alone", admitted=True),
    ),
    (
        meeting("completed", "evicted"),
        Mapped("done", end_reason="evicted", admitted=True, removed=True),
    ),
    (
        meeting("completed", "max_bot_time_exceeded"),
        Mapped("done", end_reason="max_bot_time_exceeded", admitted=True),
    ),
    (
        meeting("completed", "stopped_with_no_audio"),
        Mapped("done", end_reason="stopped_with_no_audio", admitted=True),
    ),
    (
        meeting("completed", "stopped_before_admission"),
        Mapped("cancelled", end_reason="stopped_before_admission"),
    ),
    (
        meeting("failed", "awaiting_admission_rejected", failure_stage="awaiting_admission"),
        Mapped("failed", "admission_denied", end_reason="awaiting_admission_rejected"),
    ),
    (
        meeting("failed", "awaiting_admission_timeout", failure_stage="awaiting_admission"),
        Mapped("failed", "admission_timeout", end_reason="awaiting_admission_timeout"),
    ),
    (
        meeting("failed", "auth_session_missing"),
        Mapped("failed", "auth_required", end_reason="auth_session_missing"),
    ),
    (
        meeting("failed", "join_failure", failure_stage="active", start_time=STARTED),
        Mapped("failed", "internal_error", end_reason="join_failure", admitted=True),
    ),
    (
        meeting(
            "failed", "join_failure", failure_stage="joining",
            data={"join_evidence": {"reason": "never_reached_lobby", "attribution": "system_fault"}},
        ),
        Mapped("failed", "ui_changed", end_reason="join_failure"),
    ),
    (
        meeting(
            "failed", "join_failure", failure_stage="joining",
            data={"join_evidence": {"reason": "navigation_failure", "detail": "net::ERR_ABORTED"}},
        ),
        Mapped("failed", "meeting_not_found", end_reason="join_failure"),
    ),
    (
        meeting(
            "failed", "join_failure", failure_stage="requested",
            data={"join_evidence": {"reason": "navigation_failure",
                                    "detail": "control_plane_unreachable"}},
        ),
        Mapped("failed", "internal_error", end_reason="join_failure"),
    ),
    (
        meeting(
            "failed", "join_failure",
            data={"join_evidence": {"reason": "unknown",
                                    "detail": "Zoom says: this meeting has ended"}},
        ),
        Mapped("failed", "meeting_ended", end_reason="join_failure"),
    ),
    (
        meeting(
            "failed", "join_failure",
            data={"join_evidence": {"reason": "navigation_failure",
                                    "detail": "teams_auth_redirect to login.microsoftonline.com"}},
        ),
        Mapped("failed", "auth_required", end_reason="join_failure"),
    ),
    (
        meeting("failed", "validation_error", failure_stage="requested"),
        Mapped("failed", "internal_error", end_reason="validation_error"),
    ),
    (
        meeting("failed", "evicted", start_time=STARTED),
        Mapped("done", end_reason="evicted", admitted=True, removed=True),
    ),
    (
        meeting("failed", None, data={"join_evidence": {"reason": "stopped_while_joining",
                                                        "attribution": "user_action"}}),
        Mapped("cancelled"),
    ),
]


@pytest.mark.parametrize(
    ("record", "expected"),
    TABLE,
    ids=[f"{r['status']}-{r['completion_reason']}-{i}" for i, (r, _) in enumerate(TABLE)],
)
def test_mapping_table(record: dict, expected: Mapped) -> None:
    assert map_platform(record) == expected


def test_teams_join_disabled_is_blocked_by_policy() -> None:
    """04-02: Teams kept "Join now" disabled; the bot never reached the lobby."""
    message = (
        'teams_join_disabled: Teams kept "Join now" disabled on the pre-join screen, so the bot '
        "never reached the lobby"
    )
    hoisted = meeting("failed", "join_failure", failure_stage="joining", data={"reason": message})
    detailed = meeting(
        "failed", "join_failure", failure_stage="joining",
        data={"last_error": {"exit_code": 1, "reason": message, "error_details": None}},
    )
    for record in (hoisted, detailed):
        mapped = map_platform(record)
        assert (mapped.status, mapped.failure_reason) == ("failed", "blocked_by_policy")


def test_zoom_anti_bot_wall_is_blocked_by_policy_not_a_host_denial() -> None:
    """04-03 Finding G: the wall arrives as `awaiting_admission_rejected`."""
    record = meeting(
        "failed", "awaiting_admission_rejected", failure_stage="awaiting_admission",
        data={"join_evidence": {
            "reason": "awaiting_admission_rejected", "attribution": "exogenous_platform",
            "detail": "[Zoom Web] zoom_requires_rtms: meeting/account blocks automated browser joins",
        }},
    )
    assert map_platform(record).failure_reason == "blocked_by_policy"
    record["data"]["join_evidence"]["attribution"] = "host_action"
    assert map_platform(record).failure_reason == "blocked_by_policy"  # the detail alone decides


def test_a_host_denial_without_policy_evidence_stays_admission_denied() -> None:
    record = meeting(
        "failed", "awaiting_admission_rejected",
        data={"join_evidence": {"reason": "awaiting_admission_rejected",
                                "attribution": "host_action", "detail": "removed from lobby"}},
    )
    assert map_platform(record).failure_reason == "admission_denied"


@pytest.mark.parametrize(
    ("record", "unknown"),
    [
        (meeting("teleported"), "status=teleported"),
        (meeting(None), "status=None"),
        (meeting("completed", "abducted_by_aliens"), "completion_reason=abducted_by_aliens"),
        (meeting("failed", "abducted_by_aliens"), "completion_reason=abducted_by_aliens"),
    ],
)
def test_unknown_values_fail_as_internal_error_and_are_named(record: dict, unknown: str) -> None:
    mapped = map_platform(record)
    assert (mapped.status, mapped.failure_reason, mapped.unknown) == (
        "failed", "internal_error", unknown
    )


def test_completion_reason_falls_back_to_data() -> None:
    record = meeting("completed", None, data={"completion_reason": "left_alone"})
    assert map_platform(record).end_reason == "left_alone"


@pytest.mark.parametrize(
    ("platform", "must_mention"),
    [
        ("google_meet", "With potential risks"),
        ("teams", "Teams tenant runbook"),
        ("zoom", "Waiting room"),
    ],
)
def test_admit_instructions_name_the_bot_and_the_platform_step(platform: str, must_mention: str) -> None:
    text = admit_instructions(platform, "munshi.ai (recording)")
    assert text is not None and must_mention in text
    assert '"munshi.ai (recording)"' in text


def test_no_admit_instructions_for_an_unknown_platform() -> None:
    assert admit_instructions("jitsi", "bot") is None
