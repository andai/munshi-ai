"""The fixture sanitiser, the leak scan and the auth report, on synthetic mail only.

Identifiers are planted in headers, the calendar part, Safe Links, Proofpoint and Google wrappers,
Teams context and thread ids, GUID headers and base64 parts. After `sanitise`, the leak scan finds
none of them. The parser still reads the same meeting, and a re-signed message passes the sender
check on stub DNS. The leak scan and the auth report print counts and classes only. No network: the
library default resolvers are blocked.
"""

from __future__ import annotations

import base64
import json
import os
import re
from email.quoprimime import body_encode
from pathlib import Path
from urllib.parse import quote

import dns.resolver
import mailkit
import pytest

from andai_intake.auth.verdict import Policy, from_address, verify
from andai_intake.invite import forward, ics, links, mime
from andai_intake.testing import authreport, leakscan, sanitise

SECRET = bytes(range(32))
NOTETAKER = mailkit.NOTETAKER

CAPTURE = "capture-box@acme-widgets.example"
ORGANISER = "anandita.lindqvist@acme-widgets.example"
GUEST = "ravindra.kowalczyk@contoso-partners.example"
TENANT = "3b9e2c1d-4f5a-4e6b-9c7d-8e9f0a1b2c3d"
OID = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
NETWORK_ID = "9f8e7d6c-5b4a-4321-8fed-cba987654321"
THREAD_TOKEN = base64.b64encode(b"5a6f4ab1-c456-4d78-a9b0-1234567890ab").decode().rstrip("=")
TEAMS_ID = "2345678901234"
TEAMS_P = "Xy7Kq2Lm"
ZOOM_ID = "81234567890"
ZOOM_PWD = "Qw3rTy9UiOp5AsDf.1"
ZOOM_PASSCODE = "58421379"
TENANT_LABEL = "acmewidgets"
MEET_CODE = "kwr-tbmz-pqa"
GOID_HEAD = "040000008200E00074C5B7101A82E008"
GOID_TAIL = "B0D4C3E2F1A0DA01" + "0" * 16 + "10000000"
GOID_DATA = "8C3F1A2B4D5E6F708192A3B4C5D6E7F8"
GOOGLE_EVENT = "7kukuqrfedlm2c2b0l2u8lph48"
SAFELINKS_ID = "f1e2d3c4b5a69788796a5b4c3d2e1f00"
SAFELINKS_TIME = "638641234567891234"
PP_CUSTOMER = "euR4nLbH5Zq9wXyT"
PP_RECIPIENT = "Hk2mPq8sTv4wYz6b"
PP3_KEY = "Zx9Yw8Vu7Ts6Rq5P"
MSGID_LOCAL = "BN8PR12MB3456A1B2C3D4E5F6A7B8C9D0E1"
THREAD_INDEX = "AdtKq3Zx8Y2nB1cRT0mWq5u7XyZ1Aa"
PEER_IP = "203.0.113.7"

TERMS = [
    CAPTURE, ORGANISER, GUEST, "acme-widgets.example", "contoso-partners.example", "Anandita",
    "Lindqvist", "Ravindra", "Kowalczyk", TENANT, OID, NETWORK_ID, THREAD_TOKEN, TEAMS_ID,
    TEAMS_P, ZOOM_ID, ZOOM_PWD, ZOOM_PASSCODE, TENANT_LABEL, MEET_CODE, GOID_DATA, GOOGLE_EVENT,
    SAFELINKS_ID, SAFELINKS_TIME, PP_CUSTOMER, PP_RECIPIENT, PP3_KEY, MSGID_LOCAL, THREAD_INDEX,
    PEER_IP,
]

TEAMS_URL = (
    f"https://teams.microsoft.com/l/meetup-join/19%3ameeting_{THREAD_TOKEN}%40thread.v2/0"
    f"?context=%7b%22Tid%22%3a%22{TENANT}%22%2c%22Oid%22%3a%22{OID}%22%7d"
)
TEAMS_MEET = f"https://teams.microsoft.com/meet/{TEAMS_ID}?p={TEAMS_P}"
ZOOM_URL = f"https://{TENANT_LABEL}.zoom.us/j/{ZOOM_ID}?pwd={ZOOM_PWD}"
SAFE_DATA = (
    f"05|02|{CAPTURE}|{SAFELINKS_ID}|{TENANT}|0|0|{SAFELINKS_TIME}|Unknown|"
    "TWFpbGZsb3d8eyJWIjoiMC4wLjAwMDAifQ==|0|||"
)
SAFE_TEAMS = (
    "https://nam12.safelinks.protection.outlook.com/?url=" + quote(TEAMS_URL, safe="")
    + "&data=" + quote(SAFE_DATA, safe="")
    + "&sdata=" + quote("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AB=", safe="") + "&reserved=0"
)
PP2_TEAMS = (
    "https://urldefense.proofpoint.com/v2/url?u=" + sanitise._v2_encode(TEAMS_MEET)
    + f"&d=DwMFaQ&c={PP_CUSTOMER}&r={PP_RECIPIENT}&m=AbCdEfGhIjKl&s=MnOpQrStUvWx&e="
)
PP3_ZOOM = (
    f"https://urldefense.com/v3/__https://{TENANT_LABEL}.zoom.us/j/{ZOOM_ID}?pwd={ZOOM_PWD}"
    f"&from=anandita.lindqvist*40acme-widgets.example__;JQ!!{PP3_KEY}!AbCdEfGhIjKlMnOp$"
)
EID = base64.urlsafe_b64encode(f"{GOOGLE_EVENT} {ORGANISER}".encode()).decode().rstrip("=")
TOK = base64.urlsafe_b64encode(f"19#{CAPTURE}0123456789abcdef".encode()).decode().rstrip("=")
GOOGLE_RSVP = (
    f"https://calendar.google.com/calendar/event?action=RESPOND&eid={EID}&tok={TOK}"
    "&ctz=Asia%2FKolkata&hl=en&es=1"
)
OPTIONS = (
    f"https://teams.microsoft.com/meetingOptions/?organizerId={OID}&tenantId={TENANT}"
    f"&threadId=19_meeting_{THREAD_TOKEN}@thread.v2&messageId=0&language=en-US"
)

IST = [
    "BEGIN:VTIMEZONE", "TZID:India Standard Time", "BEGIN:STANDARD", "DTSTART:16010101T000000",
    "TZOFFSETFROM:+0530", "TZOFFSETTO:+0530", "END:STANDARD", "BEGIN:DAYLIGHT",
    "DTSTART:16010101T000000", "TZOFFSETFROM:+0530", "TZOFFSETTO:+0530", "END:DAYLIGHT",
    "END:VTIMEZONE",
]


@pytest.fixture(autouse=True)
def no_system_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("a library fell back to the system resolver")

    monkeypatch.setattr(dns.resolver, "resolve", refuse)
    monkeypatch.setattr(dns.resolver, "query", refuse)


# ─── synthetic captured mail ─────────────────────────────────────────────────────────────────────


def _fold(line: str) -> str:
    return "\r\n ".join(line[i : i + 74] for i in range(0, len(line), 74))


def _calendar(method: str, uid: str, attendees: list[tuple[str, str]], extra: list[str]) -> str:
    event = [
        "BEGIN:VEVENT",
        f"ORGANIZER;CN=Anandita Lindqvist:mailto:{ORGANISER}",
        *(
            f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN={name}:mailto:{a}"
            for name, a in attendees
        ),
        "DESCRIPTION;LANGUAGE=en-US:Hi Ravindra\\,\\n\\nJoin: <" + SAFE_TEAMS + ">\\nMeeting ID:"
        " 234 567 890 123 4\\nPasscode: " + TEAMS_P + "\\n",
        f"UID:{uid}",
        "SUMMARY;LANGUAGE=en-US:Weekly sync with Anandita",
        "DTSTART;TZID=India Standard Time:20261110T100000",
        "DTEND;TZID=India Standard Time:20261110T110000",
        "DTSTAMP:20261105T090000Z",
        "LOCATION;LANGUAGE=en-US:Microsoft Teams Meeting",
        f"X-MICROSOFT-SKYPETEAMSMEETINGURL:{TEAMS_URL}",
        *extra,
        "END:VEVENT",
    ]
    lines = ["BEGIN:VCALENDAR", f"METHOD:{method}", "PRODID:Microsoft Exchange Server 2010",
             "VERSION:2.0", *IST, *event, "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _b64(text: str) -> str:
    encoded = base64.b64encode(text.encode()).decode()
    return "\r\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))


PLAIN = (
    "Hi Ravindra,\r\n\r\nAnandita Lindqvist (" + ORGANISER + ") invites you.\r\n"
    "Join: <" + SAFE_TEAMS + ">\r\n"
    "Zoom: " + ZOOM_URL + "\r\nMeeting ID: 812 3456 7890\r\nPasscode: " + ZOOM_PASSCODE + "\r\n"
    "Meet: https://meet.google.com/" + MEET_CODE + "\r\n"
    "RSVP: " + GOOGLE_RSVP + "\r\n"
)
HTML = (
    "<html><body><p>Hi Ravindra,</p>"
    f'<a href="{SAFE_TEAMS.replace("&", "&amp;")}">Join the meeting now</a>'
    f'<a href="{PP2_TEAMS.replace("&", "&amp;")}">Teams</a>'
    f'<a href="{PP3_ZOOM}">Zoom</a>'
    f'<a href="{OPTIONS.replace("&", "&amp;")}">Meeting options</a>'
    f"<p>Anandita Lindqvist &lt;{ORGANISER}&gt;</p></body></html>"
)


def outlook_invite(*, method: str = "REQUEST", instance: str = "00000000", seq: int = 0) -> bytes:
    """An Outlook Teams invite as the capture mailbox received it (Exchange Online)."""
    uid = GOID_HEAD + instance + GOID_TAIL + GOID_DATA
    extra = [f"SEQUENCE:{seq}"]
    if instance == "00000000":
        extra.append("RRULE:FREQ=WEEKLY;COUNT=4")
    else:
        extra.append("RECURRENCE-ID;TZID=India Standard Time:20261117T100000")
    calendar = _calendar(
        method,
        uid,
        [("Capture Box", CAPTURE), ("Ravindra Kowalczyk", GUEST)],
        extra,
    )
    subject = base64.b64encode(b"Weekly sync with Anandita Lindqvist").decode()
    headers = [
        "ARC-Seal: i=1; a=rsa-sha256; s=arcselector10001; d=microsoft.com; cv=none; b=AAAA",
        "ARC-Message-Signature: i=1; a=rsa-sha256; c=relaxed/relaxed; d=microsoft.com;"
        " s=arcselector10001; h=From:Date:Subject; bh=AAAA; b=AAAA",
        "ARC-Authentication-Results: i=1; mx.microsoft.com 1; spf=pass smtp.mailfrom="
        "acme-widgets.example; dmarc=pass action=none header.from=acme-widgets.example;"
        " dkim=pass header.d=acme-widgets.example; arc=none",
        f"Received: from mail.acme-widgets.example ({PEER_IP}) by BN8PR12MB3456.namprd12."
        "prod.outlook.com with HTTPS; Thu, 5 Nov 2026 09:00:00 +0000",
        "DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed; d=acme-widgets.example;"
        " s=selector1; h=From:Date:Subject; bh=AAAA; b=AAAA",
        f"Authentication-Results: spf=pass (sender IP is {PEER_IP}) smtp.mailfrom="
        "acme-widgets.example; dkim=pass header.d=acme-widgets.example",
        f"Return-Path: <{ORGANISER}>",
        f'From: "Anandita Lindqvist" <{ORGANISER}>',
        f"To: Capture Box <{CAPTURE}>, =?utf-8?q?Ravindra_Kowalczyk?= <{GUEST}>",
        f"Subject: =?utf-8?B?{subject}?=",
        f"Thread-Index: {THREAD_INDEX}==",
        "Date: Thu, 5 Nov 2026 09:00:00 +0000",
        f"Message-ID: <{MSGID_LOCAL}{instance}@BN8PR12MB3456.namprd12.prod.outlook.com>",
        f"X-MS-Exchange-Organization-Network-Message-Id: {NETWORK_ID}",
        f"X-MS-Exchange-CrossTenant-Id: {TENANT}",
        "X-Google-Smtp-Source: AGHT+IEabcdef",
        f"X-Correlation: {OID}",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="b1"',
    ]
    body = [
        "--b1",
        'Content-Type: multipart/alternative; boundary="b2"',
        "",
        "--b2",
        'Content-Type: text/plain; charset="utf-8"',
        "Content-Transfer-Encoding: quoted-printable",
        "",
        body_encode(PLAIN, 76, "\r\n"),
        "--b2",
        'Content-Type: text/html; charset="utf-8"',
        "Content-Transfer-Encoding: base64",
        "",
        _b64(HTML),
        "--b2--",
        "",
        "--b1",
        f'Content-Type: text/calendar; charset="utf-8"; method={method}',
        "Content-Transfer-Encoding: base64",
        "",
        _b64(calendar),
        "--b1",
        'Content-Type: image/png; name="Anandita-signature.png"',
        'Content-Disposition: attachment; filename="Anandita-signature.png"',
        "Content-Transfer-Encoding: base64",
        "",
        _b64("not really a png, but it names " + ORGANISER),
        "--b1--",
        "",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n" + "\r\n".join(body)).encode()


def forwarded_invite() -> bytes:
    """A guest forwards the invite to the capture mailbox (Gmail, an inbox rule): the notetaker is
    not an attendee, two ARC sets (the forward and the capture hop)."""
    calendar = _calendar(
        "REQUEST", f"{GOOGLE_EVENT}@google.com", [("Ravindra Kowalczyk", GUEST)], ["SEQUENCE:0"]
    )
    aar1 = (
        "i=1; mx.google.com; dkim=pass header.i=@contoso-partners.example header.s=sel"
        " header.b=AbCd; spf=pass (google.com: domain of " + GUEST + " designates " + PEER_IP
        + " as permitted sender) smtp.mailfrom=" + GUEST + "; dmarc=pass (p=REJECT sp=REJECT"
        " dis=NONE) header.from=contoso-partners.example"
    )
    aar2 = (
        "i=2; mx.google.com; dkim=pass header.i=@contoso-partners.example; arc=pass (i=1);"
        " dmarc=pass (p=REJECT) header.from=contoso-partners.example"
    )
    headers = [
        "ARC-Seal: i=2; a=rsa-sha256; t=1; cv=pass; d=google.com; s=arc-20240605; b=AAAA",
        "ARC-Message-Signature: i=2; a=rsa-sha256; d=google.com; s=arc-20240605; b=AAAA",
        f"ARC-Authentication-Results: {aar2}",
        f"Received: from mail.contoso-partners.example ({PEER_IP})",
        "ARC-Seal: i=1; a=rsa-sha256; t=1; cv=none; d=google.com; s=arc-20240605; b=AAAA",
        "ARC-Message-Signature: i=1; a=rsa-sha256; d=google.com; s=arc-20240605; b=AAAA",
        f"ARC-Authentication-Results: {aar1}",
        "DKIM-Signature: v=1; a=rsa-sha256; d=contoso-partners.example; s=sel; h=from:to;"
        " bh=AAAA; b=AAAA",
        f"Return-Path: <{GUEST}>",
        f"Delivered-To: {CAPTURE}",
        f"X-MS-Exchange-Inbox-Rules-Loop: {CAPTURE}",
        f"From: Ravindra Kowalczyk <{GUEST}>",
        f"To: Capture Box <{CAPTURE}>",
        "Subject: Fwd: Weekly sync with Anandita",
        "Date: Thu, 5 Nov 2026 09:05:00 +0000",
        "Message-ID: <CAKx0Ab1Cd2Ef3Gh4Ij5Kl6Mn7@mail.gmail.com>",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="f1"',
    ]
    body = [
        "--f1",
        'Content-Type: text/plain; charset="utf-8"',
        "",
        "Forwarding Anandita's invite. Meet: https://meet.google.com/" + MEET_CODE,
        "RSVP: " + GOOGLE_RSVP,
        "--f1",
        'Content-Type: application/ics; name="invite.ics"',
        'Content-Disposition: attachment; filename="invite.ics"',
        "Content-Transfer-Encoding: base64",
        "",
        _b64(calendar),
        "--f1--",
        "",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n" + "\r\n".join(body)).encode()


def _mapping(secret: bytes = SECRET, *messages: bytes) -> sanitise.Mapping:
    mapping = sanitise.Mapping(secret, [CAPTURE])
    for raw in messages:
        mapping.discover(raw)
    return mapping


def _corpus() -> tuple[sanitise.Mapping, list[sanitise.Sanitised]]:
    originals = [outlook_invite(), outlook_invite(method="CANCEL", instance="07EA0B11", seq=1),
                 forwarded_invite()]
    mapping = _mapping(SECRET, *originals)
    return mapping, [sanitise.sanitise(raw, mapping) for raw in originals]


def _scan(terms: list[str], *paths: Path) -> leakscan.Report:
    return leakscan.scan(list(enumerate(terms, start=1)), paths)


def _structure(raw: bytes) -> list[tuple[str, str | None, str | None]]:
    from email import message_from_bytes

    return [
        (p.get_content_type(), p.get("Content-Transfer-Encoding"), p.get_content_charset())
        for p in message_from_bytes(raw).walk()
    ]


# ─── rewriting ───────────────────────────────────────────────────────────────────────────────────


def test_every_planted_identifier_is_in_the_originals_and_none_in_the_sanitised(
    tmp_path: Path,
) -> None:
    originals = [outlook_invite(), forwarded_invite()]
    for index, raw in enumerate(originals):
        (tmp_path / f"original-{index}.eml").write_bytes(raw)
    for term in TERMS:  # the planting works, and the scan sees each form it was planted in
        assert _scan([term], tmp_path).total > 0, term

    _, results = _corpus()
    out = tmp_path / "out"
    out.mkdir()
    for index, result in enumerate(results):
        (out / f"fixture-{index}.eml").write_bytes(result.raw)
        (out / f"fixture-{index}.sign.json").write_text(json.dumps(result.sign_record()))
    report = _scan(TERMS, out)
    assert report.total == 0, leakscan.render(report)


def test_structure_time_zones_method_and_uid_shape_are_kept() -> None:
    original = outlook_invite()
    _, (result, *_rest) = _corpus()
    assert _structure(result.raw) == _structure(original)

    before = ics.parse(original, [CAPTURE])
    after = ics.parse(result.raw, [NOTETAKER])
    assert len(before) == len(after) == 1
    old, new = before[0], after[0]
    assert (new.method, new.start, new.end, new.rrule, new.sequence) == (
        old.method, old.start, old.end, old.rrule, old.sequence
    )
    assert str(new.zone) == str(old.zone) and new.floating is False
    assert old.start.isoformat() == "2026-11-10T04:30:00+00:00"
    assert "TZID=India Standard Time" in mime.extract(result.raw).calendars[0].text
    # The GOID keeps its class id, instance date, creation time and size; its data is mapped.
    assert len(new.uid) == len(old.uid) and new.uid[:80] == old.uid[:80]
    assert new.uid[80:] != old.uid[80:] and re.fullmatch(r"[0-9A-F]+", new.uid)
    assert new.organizer_name != old.organizer_name and new.organizer_name.istitle()


def test_a_message_with_nothing_to_rewrite_comes_out_byte_for_byte() -> None:
    raw = (
        "From: member@a.example.test\r\nTo: notetaker@munshi.test\r\nSubject: Weekly sync\r\n"
        'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="zz"\r\n\r\n'
        "preamble\r\n--zz\r\nContent-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\nJoin the meeting=3D now\r\n"
        "--zz\r\nContent-Type: text/calendar; method=REQUEST\r\nContent-Transfer-Encoding: base64"
        "\r\n\r\n" + _b64("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n")
        + "\r\n--zz--\r\nepilogue\r\n"
    ).encode()
    mapping = sanitise.Mapping(SECRET, [NOTETAKER])
    assert sanitise.sanitise(raw, mapping).raw == raw
    assert set(mapping.counts().values()) == {0}


def test_the_capture_mailbox_becomes_the_notetaker_so_the_forward_flags_stay_true() -> None:
    _, (invite, _cancel, forwarded) = _corpus()
    event = ics.parse(invite.raw, [NOTETAKER])[0]
    assert NOTETAKER in [a.address for a in event.attendees]
    assert event.is_forwarded_copy is False and event.auto_forwarded is False

    copy = ics.parse(forwarded.raw, [NOTETAKER])[0]
    assert copy.is_forwarded_copy is True and copy.auto_forwarded is True
    headers = mime.extract(forwarded.raw).headers
    assert headers["X-MS-Exchange-Inbox-Rules-Loop"] == NOTETAKER
    assert headers["Delivered-To"] == NOTETAKER
    assert forward.auto_forwarded(headers)


def test_one_stable_mapping_across_a_scenario_and_runs() -> None:
    _, (invite, cancel, _forwarded) = _corpus()
    series = ics.parse(invite.raw, [NOTETAKER])[0]
    instance = ics.parse(cancel.raw, [NOTETAKER])[0]
    assert instance.method is ics.Method.CANCEL and instance.recurrence_id is not None
    assert series.uid != instance.uid and series.uid_norm == instance.uid_norm
    assert series.join_url == instance.join_url

    # Same key and corpus, discovered in another order: the same fixtures. Another key: others.
    corpus = [outlook_invite(), outlook_invite(method="CANCEL", instance="07EA0B11", seq=1),
              forwarded_invite()]
    again = sanitise.sanitise(outlook_invite(), _mapping(SECRET, *reversed(corpus)))
    assert again.raw == invite.raw
    other = sanitise.sanitise(outlook_invite(), _mapping(b"k" * 32, *corpus))
    assert other.raw != invite.raw


def test_join_links_unwrap_to_the_same_platform_and_path_shape() -> None:
    mapping, (invite, *_rest) = _corpus()
    event = ics.parse(invite.raw, [NOTETAKER])[0]
    assert event.platform == "teams" and event.join_source == "X-MICROSOFT-SKYPETEAMSMEETINGURL"
    assert event.join_url.startswith("https://teams.microsoft.com/l/meetup-join/19%3ameeting_")
    assert THREAD_TOKEN not in event.join_url and TENANT not in event.join_url

    text = mapping.text(HTML)
    found = {name: [] for name in ("safelinks", "proofpoint_v2", "proofpoint_v3")}
    for url in links.urls_in(text):
        unwrapped = links.unwrap(url)
        assert unwrapped is not None, "a rewritten wrapper no longer decodes"
        target, wrappers = unwrapped
        if wrappers:
            found[wrappers[0]].append(target)
    teams, = found["safelinks"]
    assert links.accept(teams)[1] == "teams" and "%3a%22" in teams  # inner encoding kept
    meet, = found["proofpoint_v2"]
    assert re.fullmatch(r"https://teams\.microsoft\.com/meet/[1-9]\d{12}\?p=[A-Za-z0-9]{8}", meet)
    zoom, = found["proofpoint_v3"]
    assert re.fullmatch(
        r"https://tenant[0-9a-f]{6}\.zoom\.us/j/[1-9]\d{10}\?pwd=[A-Za-z0-9]{16}\.\d"
        r"&from=[a-z]+\.[a-z]+%40[a-z]+[0-9a-f]{4}\.example\.test",
        zoom,
    )
    assert links.accept(zoom)[1] == "zoom"
    rsvp = links.urls_in(mapping.text(GOOGLE_RSVP))[0]
    eid = re.search(r"eid=([^&]+)", rsvp).group(1)
    decoded = base64.urlsafe_b64decode(eid + "=" * (-len(eid) % 4)).decode()
    assert decoded.count(" ") == 1 and decoded.endswith(".example.test")


def test_meeting_ids_and_passcodes_are_mapped_in_spaced_text_too() -> None:
    mapping = _mapping(SECRET, outlook_invite())
    text = mapping.text(PLAIN)
    assert "812 3456 7890" not in text and ZOOM_PASSCODE not in text
    spaced = re.search(r"Meeting ID: (\d{3} \d{4} \d{4})", text).group(1)
    zoom_id = re.search(r"/j/(\d+)", text).group(1)
    assert spaced.replace(" ", "") == zoom_id  # one mapping for both forms


def test_auth_and_vendor_headers_are_dropped_and_others_rewritten() -> None:
    _, (invite, *_rest) = _corpus()
    head = invite.raw.split(b"\r\n\r\n", 1)[0].decode()
    names = [line.split(":", 1)[0].lower() for line in head.split("\r\n") if ":" in line]
    for gone in ("dkim-signature", "arc-seal", "arc-authentication-results", "received",
                 "authentication-results", "x-ms-exchange-crosstenant-id", "x-google-smtp-source"):
        assert gone not in names
    assert names[:3] == ["return-path", "from", "to"]
    headers = mime.extract(invite.raw).headers
    assert headers["Thread-Index"] != THREAD_INDEX + "==" and len(headers["Thread-Index"]) == 32
    correlation = headers["X-Correlation"]
    assert re.fullmatch(r"[0-9a-f-]{36}", correlation) and correlation != OID
    assert "Anandita" not in str(headers["Subject"])
    message_id = re.fullmatch(
        r"<(\w+)@BN8PR12MB3456\.namprd12\.prod\.outlook\.com>", headers["Message-ID"], re.I
    )
    local = message_id.group(1)
    assert len(local) == len(MSGID_LOCAL) + 8 and MSGID_LOCAL.lower() not in local.lower()
    attachment = next(
        p for p in mime.extract(invite.raw).headers.walk() if p.get_content_type() == "image/png"
    )
    assert attachment.get_payload(decode=True) == sanitise.PLACEHOLDER


def test_resigned_dkim_passes_the_sender_check_on_stub_dns() -> None:
    _, (invite, _cancel, _forwarded) = _corpus()
    assert [s.kind for s in invite.steps] == ["dkim", "arc"]
    kit = mailkit.Kit()
    signed = sanitise.resign(invite, kit)
    _, from_domain, _ = from_address(signed)
    assert from_domain.endswith(".example.test")
    kit.dmarc(from_domain)
    kit.spf(from_domain)
    auth = verify(signed, invite.mail_from, "192.0.2.10", None, Policy(resolver=kit.zone))
    assert auth.sender_auth == "dmarc_pass", auth.summary
    signature = auth.summary["dkim"][0]
    assert signature == {"d": from_domain, "s": "sel", "result": "pass", "reason": None}
    assert auth.summary["arc"]["cv"] == "pass" and auth.summary["arc"]["sealer"] == "microsoft.com"
    # The parser reads the signed message as it read the unsigned one.
    assert ics.parse(signed, [NOTETAKER])[0].start == ics.parse(invite.raw, [NOTETAKER])[0].start


def test_resealed_arc_of_a_forward_verifies_and_can_be_trusted() -> None:
    _, (_invite, _cancel, forwarded) = _corpus()
    assert [(s.kind, s.domain) for s in forwarded.steps][1:] == [
        ("arc", "google.com"), ("arc", "google.com")
    ]
    _, from_domain, _ = from_address(forwarded.raw)
    first_aar = forwarded.steps[1].results
    assert f"header.from={from_domain}" in first_aar and PEER_IP not in first_aar

    kit = mailkit.Kit()
    kit.dmarc(from_domain)
    kit.spf(from_domain)
    signed = sanitise.resign(forwarded, kit)
    auth = verify(signed, forwarded.mail_from, "192.0.2.10", None, Policy(resolver=kit.zone))
    assert auth.sender_auth == "dmarc_pass"
    assert auth.summary["arc"] == {
        "cv": "pass", "sealer": "google.com", "instances": 2, "trusted": False,
        "reason": "sealer_not_allowlisted",
    }
    # Without the DKIM step only the seal can vouch: trusted once google.com is allowlisted.
    arc_only = sanitise.Sanitised(
        forwarded.raw, tuple(s for s in forwarded.steps if s.kind == "arc"), forwarded.mail_from
    )
    sealed = sanitise.resign(arc_only, kit)
    policy = Policy(resolver=kit.zone, arc_sealers=frozenset({"google.com"}))
    assert verify(sealed, forwarded.mail_from, "192.0.2.10", None, policy).sender_auth == (
        "arc_trusted"
    )


def test_sign_record_round_trips() -> None:
    _, (_invite, _cancel, forwarded) = _corpus()
    record = json.loads(json.dumps(forwarded.sign_record()))
    assert sanitise.steps_from_record(record) == forwarded.steps
    assert record["mail_from"] == forwarded.mail_from and forwarded.mail_from.endswith(".test")


def test_domains_tenants_and_platforms() -> None:
    mapping = sanitise.Mapping(SECRET, [CAPTURE])
    assert mapping.map_host("meet.google.com") == "meet.google.com"
    assert mapping.map_host("nam12.safelinks.protection.outlook.com").startswith("nam12.")
    assert mapping.map_host("us02web.zoom.us") == "us02web.zoom.us"
    vanity = mapping.map_host("acmewidgets.zoom.us")
    assert re.fullmatch(r"tenant[0-9a-f]{6}\.zoom\.us", vanity)
    assert mapping.map_host("acmewidgets.onmicrosoft.com").endswith(".onmicrosoft.com")
    org = mapping.map_host("acme.co.in")
    assert mapping.map_host("mail.acme.co.in") == "mail." + org and org.endswith(".example.test")
    assert mapping.map_address("no-reply@zoom.us") == "no-reply@zoom.us"
    assert mapping.map_address("someone@gmail.com").endswith("@gmail.com")
    assert mapping.map_address(CAPTURE.upper()) == NOTETAKER
    assert mapping.map_address(NOTETAKER) == NOTETAKER


def test_terms_file_identifiers_are_rewritten_everywhere() -> None:
    mapping = sanitise.Mapping(SECRET, [CAPTURE])
    for term in ("Quillfeather", "orchid-labs.example", "+91 98450 12345", "Pz8Kq2Lm9"):
        mapping.add_term(term)
    text = mapping.terms("Call Quillfeather (QUILLFEATHER) at +91 98450 12345, code Pz8Kq2Lm9, "
                         "see https://wiki.orchid-labs.example/x")
    for gone in ("Quillfeather", "QUILLFEATHER", "98450 12345", "Pz8Kq2Lm9", "orchid-labs"):
        assert gone not in text
    assert re.search(r"\(([A-Z]+)\)", text).group(1).isupper()  # case kept


def test_the_command_writes_fixtures_and_sign_records_and_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    originals = tmp_path / "originals"
    originals.mkdir()
    (originals / "O-01.eml").write_bytes(outlook_invite())
    (originals / "G-08.eml").write_bytes(forwarded_invite())
    (tmp_path / "terms.txt").write_text("# extra\nQuillfeather\n")
    out = tmp_path / "out"
    args = [
        "--secret", str(tmp_path / "key"), "--capture", CAPTURE,
        "--terms", str(tmp_path / "terms.txt"), "--terms-out", str(tmp_path / "found.txt"),
        "--out", str(out), str(originals / "O-01.eml"), str(originals / "G-08.eml"),
    ]
    assert sanitise.main(args) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("sanitised 2 message(s); identifiers mapped: email ")
    assert not any(term.lower() in printed.lower() for term in TERMS)
    assert sorted(p.name for p in out.iterdir()) == [
        "G-08.eml", "G-08.sign.json", "O-01.eml", "O-01.sign.json"
    ]
    assert os.stat(tmp_path / "key").st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "found.txt").st_mode & 0o777 == 0o600
    found = (tmp_path / "found.txt").read_text().splitlines()
    assert {CAPTURE, ORGANISER, GUEST, "quillfeather"} <= set(found)
    assert _scan(TERMS, out).total == 0
    first = (out / "O-01.eml").read_bytes()
    assert sanitise.main(args) == 0  # the same key gives the same fixtures
    assert (out / "O-01.eml").read_bytes() == first


# ─── leak scan ───────────────────────────────────────────────────────────────────────────────────


def test_leakscan_finds_a_term_in_each_form(tmp_path: Path) -> None:
    term = ORGANISER
    payload = b"xx" + term.encode() + b"yy"
    samples = {
        "plain": term,
        "lower-case": term.upper(),
        "url-encoded": quote(term, safe=""),
        "url-decoded": term,  # searched for the decoded form of an encoded term below
        "base64": "\r\n".join(
            base64.b64encode(b"\0" * k + payload).decode() for k in range(3)
        ),
    }
    for form, text in samples.items():
        (tmp_path / f"{form}.txt").write_text(text)
        searched = quote(term, safe="") if form == "url-decoded" else term
        report = leakscan.scan([(1, searched)], [tmp_path / f"{form}.txt"])
        assert report.hits[form] >= 1, (form, report.hits)
    assert leakscan.scan([(1, term)], [tmp_path / "lower-case.txt"]).hits["plain"] == 0
    assert leakscan.scan([(1, term)], [tmp_path / "base64.txt"]).hits["base64"] == 3


def test_leakscan_reads_mail_decoded(tmp_path: Path) -> None:
    long_line = "x" * 70 + " " + ORGANISER + " " + "y" * 40
    raw = (
        "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable"
        "\r\n\r\n" + body_encode(long_line, 76, "\r\n") + "\r\n"
    ).encode()
    assert ORGANISER.encode() not in raw  # the soft break splits it
    (tmp_path / "split.eml").write_bytes(raw)
    assert leakscan.scan([(1, ORGANISER)], [tmp_path]).total >= 1


def test_leakscan_prints_counts_and_line_numbers_never_terms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "terms.txt").write_text(f"# originals\n{ORGANISER}\nab\n{TENANT}\n")
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "leak.txt").write_text(f"contact {quote(ORGANISER, safe='')}")
    (tmp_path / "files" / "clean.txt").write_text("nothing here")
    code = leakscan.main(["--terms", str(tmp_path / "terms.txt"), str(tmp_path / "files")])
    printed = capsys.readouterr().out
    assert code == 1
    assert ORGANISER not in printed and "acme" not in printed and TENANT not in printed
    assert "2 file(s), 2 term(s) (1 skipped" in printed
    assert "url-encoded: 1" in printed and "terms on line(s) 2" in printed
    (tmp_path / "files" / "leak.txt").write_text("clean now")
    assert leakscan.main(["--terms", str(tmp_path / "terms.txt"), str(tmp_path / "files")]) == 0


# ─── auth report ─────────────────────────────────────────────────────────────────────────────────

MEMBER = "member-corp.example"
FORWARDER = "relay-forwarder.example"


def _member_mail(kit: mailkit.Kit, *, sign: str | None, return_path: str) -> bytes:
    raw = mailkit.message(
        f"Priyamvada Okonkwo <priyamvada@{MEMBER}>",
        headers=(("Return-Path", f"<{return_path}>"),),
        body="See you there.\r\n",
    )
    return kit.dkim(raw, sign) if sign else raw


def test_authreport_prints_redacted_results_per_family(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = mailkit.Kit()
    kit.dmarc(MEMBER)
    kit.spf(MEMBER, "ip4:192.0.2.0/24")
    nodmarc = "no-dmarc-corp.example"
    samples = {
        # own DKIM: passes by DKIM
        "O-01.eml": _member_mail(kit, sign=MEMBER, return_path=f"bounce@{MEMBER}"),
        # no signature, envelope aligned: only SPF could have passed at the original hop
        "O-02.eml": _member_mail(kit, sign=None, return_path=f"bounce@mail.{MEMBER}"),
        # signed by google.com only (no own DKIM): rejected
        "G-10.eml": _member_mail(kit, sign="google.com",
                                 return_path="x@calendar-server.bounces.google.com"),
        # forwarded: the member's signature broken by the forwarder, google.com seals
        "G-08.eml": kit.arc(
            mailkit.append_to_body(_member_mail(kit, sign=MEMBER, return_path=f"b@{MEMBER}"),
                                   "-- forwarded\r\n"),
            "google.com",
            f"dmarc=pass header.from={MEMBER}",
        ),
        # forwarded through a forwarder that is not a platform
        "G-09.eml": kit.arc(
            _member_mail(kit, sign=None, return_path=f"srs@{FORWARDER}"),
            FORWARDER,
            f"dmarc=pass header.from={MEMBER}",
        ),
        "X-01.eml": kit.dkim(
            mailkit.message(f"Someone <someone@{nodmarc}>"), nodmarc
        ),
    }
    for name, raw in samples.items():
        (tmp_path / name).write_bytes(raw)
    (tmp_path / "unlisted.eml").write_bytes(samples["O-01.eml"])
    families = {"O-01": "outlook", "O-02": "outlook", "G-10": "google-no-dkim",
                "G-08": "gmail-forward", "G-09": "gmail-forward", "X-01": "other"}
    record = {
        "capture_provider": "google",
        "files": {f"{k}.eml": {"scenario": k, "family": v} for k, v in families.items()}
        | {"missing.eml": {"scenario": "M-01", "family": "outlook"}},
    }
    (tmp_path / "record.json").write_text(json.dumps(record))
    monkeypatch.setattr(
        "andai_intake.auth.verdict.configured_policy", lambda: Policy(resolver=kit.zone)
    )
    code = authreport.main(["--capture-record", str(tmp_path / "record.json"), str(tmp_path)])
    printed = capsys.readouterr().out
    assert code == 0

    rows = {line.split(" | ")[0].lstrip("| "): line for line in printed.splitlines()
            if line.startswith("| ") and not line.startswith("| Scenario")}
    assert "| dmarc_pass | dkim | From domain=pass |" in rows["O-01"]
    assert f"| {authreport.NOT_EVALUABLE} |" in rows["O-02"]
    assert "| fail (not_aligned) |" in rows["G-10"] and "google.com=pass" in rows["G-10"]
    assert "From domain=fail (body_hash_mismatch)" in rows["G-08"]
    assert "aligned DKIM did not verify" in rows["G-08"]
    assert "i=1 google.com (capture hop)" in rows["G-08"]
    assert "would trust: google.com" in rows["G-08"]
    assert "i=1 other domain" in rows["G-09"] and "would trust: other domain" in rows["G-09"]
    assert "| fail (no_dmarc_record) |" in rows["X-01"] and "| no record |" in rows["X-01"]
    assert "- outlook: 2 sample(s); dmarc_pass 1, not evaluable (replay) 1" in printed
    assert "listed but missing: 1; not listed: 1" in printed
    for secret in (MEMBER, FORWARDER, nodmarc, "priyamvada", "okonkwo", "someone", "bounce",
                   "sel", "See you there", "Weekly sync"):
        assert secret.lower() not in printed.lower(), secret


def test_authreport_with_a_candidate_sealer(tmp_path: Path) -> None:
    kit = mailkit.Kit()
    kit.dmarc(MEMBER)
    raw = kit.arc(_member_mail(kit, sign=None, return_path=f"x@{FORWARDER}"), "google.com",
                  f"dmarc=pass header.from={MEMBER}")
    row = authreport.evaluate(
        raw, Policy(resolver=kit.zone, arc_sealers=frozenset({"google.com"})),
        scenario="G-09", family="gmail-forward", capture_sealer=None,
    )
    assert (row.verdict, row.mechanism) == ("arc_trusted", "arc")
    assert row.arc == "cv=pass; i=1 google.com"
