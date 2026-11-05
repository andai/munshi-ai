"""MIME extraction of invites: inline and attached calendars once, one level of forwarded message,
caps that end in `parse_error`, other attachments ignored unread."""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP

import pytest

from andai_intake.invite import InviteParseError, mime, parse
from invitekit import NOTETAKER, calendar, message, vevent


def test_inline_and_attachment_twins_give_one_event():
    ics = calendar(vevent())
    raw = message(inline=ics, attachment=ics)
    extracted = mime.extract(raw)
    assert len(extracted.calendars) == 1  # byte-identical calendars collapse
    events = parse(raw, [NOTETAKER])
    assert len(events) == 1
    assert events[0].uid == "evt-1@acme.test"


def test_inline_and_attachment_that_differ_in_bytes_still_give_one_event():
    inline = calendar(vevent())
    attached = calendar(vevent()).replace("PRODID:-//munshi tests//EN", "PRODID:x")
    raw = message(inline=inline, attachment=attached)
    assert len(mime.extract(raw).calendars) == 2
    assert len(parse(raw, [NOTETAKER])) == 1  # same UID, RECURRENCE-ID, SEQUENCE, DTSTAMP


def test_inline_part_method_parameter_and_attachment_flag():
    raw = message(inline=calendar(vevent(), method=None), method="CANCEL")
    (part,) = mime.extract(raw).calendars
    assert part.method_param == "CANCEL"
    assert part.attachment is False
    (event,) = parse(raw, [NOTETAKER])
    assert event.method == "CANCEL"  # no METHOD in the VCALENDAR: the Content-Type parameter


def test_forward_as_attachment_is_unwrapped_one_level():
    inner = message(inline=calendar(vevent(uid="inner@acme.test")))
    raw = message(body="FYI, see the attached invite.", forwarded=inner)
    extracted = mime.extract(raw)
    assert len(extracted.forwarded_headers) == 1
    (event,) = parse(raw, [NOTETAKER])
    assert event.uid == "inner@acme.test"
    assert event.via_forward_attachment is True


def test_forward_inside_a_forward_is_ignored():
    innermost = message(inline=calendar(vevent(uid="deep@acme.test")))
    middle = message(body="fwd", forwarded=innermost)
    raw = message(body="fwd fwd", forwarded=middle)
    assert mime.extract(raw).calendars == []
    assert parse(raw, [NOTETAKER]) == []


def test_text_bodies_are_kept_for_the_link_fallback():
    raw = message(inline=calendar(vevent()), body="plain", html="<p>html</p>")
    bodies = mime.extract(raw).bodies
    assert [b.content_type for b in bodies] == ["text/plain", "text/html"]


def test_zip_and_html_attachments_are_not_decoded(monkeypatch):
    raw = message(
        inline=calendar(vevent()),
        other_attachments=[
            ("application/zip", "bomb.zip", b"PK\x03\x04" + b"\0" * 2048),
            ("text/html", "page.html", b"<a href='https://meet.google.com/abc-defg-hij'>x</a>"),
        ],
    )
    decoded: list[str] = []
    real = mime._decoded

    def spy(part, limit):
        decoded.append(part.get_content_type())
        return real(part, limit)

    monkeypatch.setattr(mime, "_decoded", spy)
    extracted = mime.extract(raw)
    assert "application/zip" not in decoded
    assert [b.content_type for b in extracted.bodies] == ["text/plain"]  # html attachment skipped
    assert len(extracted.calendars) == 1


def _many_parts(count: int) -> bytes:
    msg = EmailMessage()
    msg["From"] = "ana@acme.test"
    msg.set_content("x")
    for i in range(count):
        msg.add_attachment(b"x", maintype="application", subtype="octet-stream", filename=f"{i}")
    return msg.as_bytes(policy=SMTP)


def test_part_cap():
    mime.extract(_many_parts(mime.MAX_PARTS - 2))  # root + body + attachments = 50
    with pytest.raises(mime.MimeError):
        mime.extract(_many_parts(mime.MAX_PARTS))
    with pytest.raises(InviteParseError) as err:
        parse(_many_parts(mime.MAX_PARTS), [NOTETAKER])
    assert err.value.reason == "parse_error"


def _nested(depth: int) -> bytes:
    boundary = [f"b{i}" for i in range(depth)]
    head = "From: ana@acme.test\r\nMIME-Version: 1.0\r\n"
    body = "text/plain\r\n\r\nleaf\r\n"
    for b in reversed(boundary):
        body = (
            f'multipart/mixed; boundary="{b}"\r\n\r\n--{b}\r\nContent-Type: {body}--{b}--\r\n'
        )
    return (head + "Content-Type: " + body).encode()


def test_depth_cap():
    mime.extract(_nested(mime.MAX_DEPTH))
    with pytest.raises(mime.MimeError):
        mime.extract(_nested(mime.MAX_DEPTH + 1))


def test_calendar_size_cap():
    big = calendar(vevent(summary="x" * 70, extra=[f"COMMENT:{'y' * 1000}"] * 1100))
    assert len(big) > mime.MAX_ICS_BYTES
    with pytest.raises(InviteParseError):
        parse(message(attachment=big), [NOTETAKER])


def test_vevent_cap():
    events = [vevent(uid=f"e{i}@acme.test") for i in range(201)]
    with pytest.raises(InviteParseError) as err:
        parse(message(attachment=calendar(*events)), [NOTETAKER])
    assert "VEVENT" in str(err.value)
    assert len(parse(message(attachment=calendar(*events[:200])), [NOTETAKER])) == 200


def test_no_calendar_gives_no_events():
    assert parse(message(body="just a note"), [NOTETAKER]) == []


def test_unreadable_calendar_and_calendar_without_events_are_parse_errors():
    with pytest.raises(InviteParseError):
        parse(message(attachment="BEGIN:VCALENDAR\r\nEND:VEVENT\r\n"), [NOTETAKER])
    with pytest.raises(InviteParseError):
        parse(message(attachment=calendar()), [NOTETAKER])


def test_event_without_uid_is_skipped_others_kept():
    good = vevent(uid="good@acme.test")
    bad = vevent(uid="").replace("UID:\r\n", "")
    (event,) = parse(message(attachment=calendar(bad, good)), [NOTETAKER])
    assert event.uid == "good@acme.test"


def test_time_cap_is_a_parse_error():
    with pytest.raises(InviteParseError):
        parse(message(attachment=calendar(vevent())), [NOTETAKER], deadline_s=-1)
