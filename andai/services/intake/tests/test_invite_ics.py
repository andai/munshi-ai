"""Event fields of a parsed invite: METHOD, UID normalisation, RECURRENCE-ID, SEQUENCE, DTSTAMP,
ORGANIZER/ATTENDEEs, SUMMARY, and the forward flags."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from andai_intake.invite import Method, normalise_uid, parse
from andai_intake.invite.forward import AUTO_FORWARD_HEADERS, auto_forwarded, is_forwarded_copy
from invitekit import NOTETAKER, calendar, message, vevent

SERIES_GOID = (
    "040000008200E00074C5B7101A82E008"
    "00000000"
    "D0B5AD4E8F2EDB01000000000000000010000000F6B0E5C6E4C8B54FA3A4B8B6A6A0E7F2"
)
INSTANCE_GOID = SERIES_GOID[:32] + "07EA0B0A" + SERIES_GOID[40:]  # 2026-11-10


def one(raw: bytes, notetaker: tuple[str, ...] = (NOTETAKER,)):
    (event,) = parse(raw, notetaker)
    return event


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("REQUEST", Method.REQUEST),
        ("CANCEL", Method.CANCEL),
        ("REPLY", Method.REPLY),
        ("COUNTER", Method.COUNTER),
        ("REFRESH", Method.REFRESH),
        ("ADD", Method.ADD),
        ("request", Method.REQUEST),
        ("PUBLISH", Method.UNKNOWN),
        ("X-WHATEVER", Method.UNKNOWN),
    ],
)
def test_method(method, expected):
    event = one(message(attachment=calendar(vevent(), method=method)))
    assert event.method == expected
    assert event.method_raw == method.upper()


def test_no_method_anywhere_is_unknown():
    event = one(message(attachment=calendar(vevent(), method=None)))
    assert event.method == Method.UNKNOWN
    assert event.method_raw is None


def test_exchange_goid_instance_and_series_share_a_key():
    assert normalise_uid(INSTANCE_GOID) == normalise_uid(SERIES_GOID) == SERIES_GOID
    assert normalise_uid(INSTANCE_GOID.lower()) == SERIES_GOID
    event = one(message(attachment=calendar(vevent(uid=INSTANCE_GOID))))
    assert event.uid == INSTANCE_GOID
    assert event.uid_norm == SERIES_GOID


def test_other_uids_are_kept_as_given():
    assert normalise_uid("  7kukuqrfedlm2f2t9ckbnm7oq3@google.com ") == (
        "7kukuqrfedlm2f2t9ckbnm7oq3@google.com"
    )
    short_hex = "040000008200E00074C5B7101A82E008"  # prefix only: not a full GOID
    assert normalise_uid(short_hex) == short_hex
    not_goid = "AB" * 50
    assert normalise_uid(not_goid) == not_goid


def test_recurrence_id_with_tzid():
    event = one(
        message(
            attachment=calendar(
                vevent(
                    dtstart="DTSTART;TZID=Asia/Kolkata:20261117T100000",
                    dtend="DTEND;TZID=Asia/Kolkata:20261117T110000",
                    extra=["RECURRENCE-ID;TZID=Asia/Kolkata:20261117T100000"],
                )
            )
        )
    )
    assert event.recurrence_id == datetime(2026, 11, 17, 4, 30, tzinfo=UTC)
    assert event.recurrence_range is None


def test_recurrence_id_utc_and_range():
    event = one(
        message(
            attachment=calendar(
                vevent(extra=["RECURRENCE-ID;RANGE=THISANDFUTURE:20261110T043000Z"])
            )
        )
    )
    assert event.recurrence_id == datetime(2026, 11, 10, 4, 30, tzinfo=UTC)
    assert event.recurrence_range == "THISANDFUTURE"


def test_recurrence_id_windows_tzid_and_all_day():
    event = one(
        message(
            attachment=calendar(
                vevent(extra=["RECURRENCE-ID;TZID=India Standard Time:20261117T100000"])
            )
        )
    )
    assert event.recurrence_id == datetime(2026, 11, 17, 4, 30, tzinfo=UTC)
    event = one(message(attachment=calendar(vevent(extra=["RECURRENCE-ID;VALUE=DATE:20261117"]))))
    assert event.recurrence_id == date(2026, 11, 17)


def test_sequence_defaults_to_zero_and_reads_integers():
    assert one(message(attachment=calendar(vevent(sequence=None)))).sequence == 0
    assert one(message(attachment=calendar(vevent(sequence=3)))).sequence == 3
    bad = calendar(vevent(sequence=None, extra=["SEQUENCE:three"]))
    event = one(message(attachment=bad))
    assert event.sequence == 0


def test_dtstamp_dtstart_dtend_in_utc():
    event = one(message(attachment=calendar(vevent())))
    assert event.dtstamp == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert event.start == datetime(2026, 11, 10, 4, 30, tzinfo=UTC)
    assert event.end == datetime(2026, 11, 10, 5, 30, tzinfo=UTC)
    assert one(message(attachment=calendar(vevent(dtstamp=None)))).dtstamp is None


def test_duration_gives_the_end():
    event = one(message(attachment=calendar(vevent(dtend=None, extra=["DURATION:PT45M"]))))
    assert event.end == datetime(2026, 11, 10, 5, 15, tzinfo=UTC)
    assert one(message(attachment=calendar(vevent(dtend=None)))).end is None


def test_organizer_and_attendees():
    event = one(
        message(
            attachment=calendar(
                vevent(
                    organizer="ORGANIZER;CN=\"Ana Rao\":MAILTO:Ana@ACME.test",
                    attendees=[
                        f"mailto:{NOTETAKER.upper()}",
                        "MAILTO:bo@acme.test",
                        "mailto:not an address",
                        "urn:uuid:1234",
                    ],
                )
            )
        )
    )
    assert event.organizer == "ana@acme.test"
    assert event.organizer_name == "Ana Rao"
    assert [a.address for a in event.attendees] == [NOTETAKER, "bo@acme.test"]
    assert event.attendees[0].partstat == "NEEDS-ACTION"
    assert event.attendees[0].role == "REQ-PARTICIPANT"


def test_summary_is_sanitised_and_capped():
    event = one(
        message(attachment=calendar(vevent(summary="Hi\\n‮evil​  there" + "x" * 300)))
    )
    assert "\n" not in event.summary and "‮" not in event.summary
    assert event.summary.startswith("Hi evil there")
    assert len(event.summary) == 200


def test_is_forwarded_copy():
    assert one(message(attachment=calendar(vevent()))).is_forwarded_copy is False
    forwarded = vevent(attendees=["mailto:bo@acme.test"])
    assert one(message(attachment=calendar(forwarded))).is_forwarded_copy is True
    # a second notetaker alias on the attendee list counts as the notetaker
    alias = vevent(attendees=["mailto:notes@munshi.test"])
    event = one(message(attachment=calendar(alias)), (NOTETAKER, "Notes@munshi.test"))
    assert event.is_forwarded_copy is False
    assert is_forwarded_copy([], [NOTETAKER]) is True


@pytest.mark.parametrize(
    ("header", "value", "expected"),
    [
        ("Auto-Submitted", "auto-forwarded", True),
        ("Auto-Submitted", "Auto-Forwarded; owner-email=x@y", True),
        ("Auto-Submitted", "auto-generated", False),
        ("Auto-Submitted", "no", False),
        ("X-MS-Exchange-Inbox-Rules-Loop", "ana@acme.test", True),
        ("X-Forwarded-For", "ana@acme.test bo@other.test", True),
        ("X-Forwarded-To", "bo@other.test", True),
        ("X-Mailer", "Outlook", False),
    ],
)
def test_auto_forwarded(header, value, expected):
    event = one(message(attachment=calendar(vevent()), headers={header: value}))
    assert event.auto_forwarded is expected


def test_auto_forward_header_list_is_the_documented_one():
    assert AUTO_FORWARD_HEADERS == (
        "Auto-Submitted",
        "X-MS-Exchange-Inbox-Rules-Loop",
        "X-Forwarded-For",
        "X-Forwarded-To",
    )
    from email.message import EmailMessage

    assert auto_forwarded(EmailMessage()) is False


def test_status_and_cancelled_instance():
    event = one(message(attachment=calendar(vevent(extra=["STATUS:CANCELLED"]), method="CANCEL")))
    assert event.status == "CANCELLED"
    assert event.method == Method.CANCEL


def test_cancel_without_dtstart_is_kept():
    event = one(message(attachment=calendar(vevent(dtstart="", dtend=None), method="CANCEL")))
    assert event.start is None
    assert event.method == Method.CANCEL


def test_several_vevents_series_and_override_are_all_returned():
    series = vevent(extra=["RRULE:FREQ=WEEKLY;COUNT=4"])
    override = vevent(
        dtstart="DTSTART:20261117T060000Z",
        dtend="DTEND:20261117T070000Z",
        extra=["RECURRENCE-ID:20261117T043000Z"],
    )
    events = parse(message(attachment=calendar(series, override)), [NOTETAKER])
    assert [e.recurrence_id for e in events] == [None, datetime(2026, 11, 17, 4, 30, tzinfo=UTC)]
    assert events[0].rrule == "FREQ=WEEKLY;COUNT=4"
