"""Join links: where they are found, how wrappers are removed offline, which links are refused."""

from __future__ import annotations

import base64
import itertools
import string
from urllib.parse import quote

import pytest

from andai_intake.invite import parse
from andai_intake.invite.links import accept, join_link, unwrap, urls_in
from invitekit import NOTETAKER, calendar, message, vevent

MEET = "https://meet.google.com/abc-defg-hij"
TEAMS_CLASSIC = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NjA2ZDk0%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%2272f9%22%2c%22Oid%22%3a%2264d6%22%7d"
)
TEAMS_MEET = "https://teams.microsoft.com/meet/2468013579?p=AbCdEf123"
TEAMS_LIVE = "https://teams.live.com/meet/9876543210?p=XyZ"
ZOOM = "https://us02web.zoom.us/j/81234567890?pwd=SecretPass.1"


def link_of(**props: list[str] | str) -> str | None:
    texts = {k.replace("_", "-"): [v] if isinstance(v, str) else v for k, v in props.items()}
    found = join_link(texts, [])
    return found.url if found else None


def safelinks(url: str) -> str:
    return (
        "https://nam12.safelinks.protection.outlook.com/?url="
        + quote(url, safe="")
        + "&data=05%7C02%7Cana%40acme.test%7C1234&sdata=AbC%3D&reserved=0"
    )


def proofpoint_v2(url: str) -> str:
    escaped = quote(url, safe="").replace("-", "%2D").replace("_", "%5F")
    encoded = escaped.replace("%", "-").replace("/", "_")
    tail = "&d=DwMFaQ&c=abc&r=def&m=ghi&s=jkl&e="
    return f"https://urldefense.proofpoint.com/v2/url?u={encoded}{tail}"


def proofpoint_v1(url: str) -> str:
    return f"https://urldefense.proofpoint.com/v1/url?u={quote(url, safe='')}&k=abc%3D%0A&r=x"


RUN_CHARS = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"


def proofpoint_v3(url: str) -> str:
    """Encode like URL Defense v3: characters outside a safe set move into the base64 data; one
    becomes `*`, a run of n ≥ 2 becomes `**` and the run-length character for n."""
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/:")
    masked: list[str] = []
    taken: list[str] = []
    for unsafe, group in itertools.groupby(url, key=lambda c: c not in safe):
        chars = "".join(group)
        if not unsafe:
            masked.append(chars)
            continue
        taken.append(chars)
        masked.append("*" if len(chars) == 1 else "**" + RUN_CHARS[len(chars) - 2])
    data = base64.urlsafe_b64encode("".join(taken).encode()).decode().rstrip("=")
    return f"https://urldefense.com/v3/__{''.join(masked)}__;{data}!!ABCdef!ghIJk$"


def google_redirect(url: str) -> str:
    return f"https://www.google.com/url?q={quote(url, safe=':/')}&sa=D&source=calendar&usg=AOvVaw1"


# ─── where links are found ──────────────────────────────────────────────────────────────────────


def test_vendor_properties_come_first():
    assert link_of(X_GOOGLE_CONFERENCE=MEET, DESCRIPTION=f"Join {ZOOM}") == MEET
    assert link_of(X_MICROSOFT_SKYPETEAMSMEETINGURL=TEAMS_CLASSIC, LOCATION=ZOOM) == TEAMS_CLASSIC
    assert link_of(X_MICROSOFT_ONLINEMEETINGEXTERNALLINK=TEAMS_MEET, DESCRIPTION=MEET) == TEAMS_MEET


def test_description_then_alt_desc_then_location():
    assert link_of(DESCRIPTION=f"Join: <{TEAMS_MEET}>", LOCATION=ZOOM) == TEAMS_MEET
    alt = f'<html><a href="{TEAMS_CLASSIC.replace("&", "&amp;")}">Join</a></html>'
    assert link_of(X_ALT_DESC=alt, LOCATION=ZOOM) == TEAMS_CLASSIC
    assert link_of(LOCATION=ZOOM) == ZOOM


def test_mime_body_is_the_last_resort():
    found = join_link({"DESCRIPTION": ["no link here"]}, ["text", f"<a href='{MEET}'>Join</a>"])
    assert found is not None and found.url == MEET and found.source == "body"
    assert join_link({}, []) is None


def test_join_shaped_link_beats_other_links_to_the_same_hosts():
    teams_description = (
        "Microsoft Teams meeting\n"
        "Need help? <https://aka.ms/JoinTeamsMeeting?omkt=en-US>\n"
        f"Join the meeting now <{TEAMS_CLASSIC}>\n"
        "Meeting options "
        "<https://teams.microsoft.com/meetingOptions/?organizerId=64d6&tenantId=72f9>"
    )
    assert link_of(DESCRIPTION=teams_description) == TEAMS_CLASSIC
    zoom_description = (
        "Find your local number: https://us02web.zoom.us/u/kdXyZ\n"
        f"Join Zoom Meeting\n{ZOOM}\n"
    )
    assert link_of(DESCRIPTION=zoom_description) == ZOOM
    # nothing join-shaped anywhere: the first allowlisted link is still returned
    assert link_of(DESCRIPTION="https://us02web.zoom.us/u/kdXyZ") == (
        "https://us02web.zoom.us/u/kdXyZ"
    )


@pytest.mark.parametrize("url", [TEAMS_CLASSIC, TEAMS_MEET, TEAMS_LIVE, ZOOM, MEET])
def test_platform_forms_keep_their_query(url):
    assert link_of(DESCRIPTION=f"Join here: {url} .") == url


def test_trailing_punctuation_and_fragment():
    assert link_of(DESCRIPTION=f"({MEET}).") == MEET
    assert link_of(DESCRIPTION=f"{ZOOM}#success") == ZOOM


def test_in_a_parsed_invite():
    ics = calendar(vevent(extra=[f"X-GOOGLE-CONFERENCE:{MEET}", f"LOCATION:{ZOOM}"]))
    (event,) = parse(message(attachment=ics), [NOTETAKER])
    assert (event.join_url, event.platform, event.join_source) == (
        MEET,
        "google_meet",
        "X-GOOGLE-CONFERENCE",
    )
    body_only = message(attachment=calendar(vevent()), body=f"Join: {safelinks(TEAMS_MEET)}")
    (event,) = parse(body_only, [NOTETAKER])
    assert (event.join_url, event.platform, event.join_source) == (TEAMS_MEET, "teams", "body")
    (event,) = parse(message(attachment=calendar(vevent()), body="no link"), [NOTETAKER])
    assert event.join_url is None and event.platform is None


def test_escaped_description_in_a_parsed_invite():
    folded = "DESCRIPTION:Join Zoom Meeting\\n" + ZOOM.replace(",", "\\,") + "\\n\\nMeeting ID"
    (event,) = parse(message(attachment=calendar(vevent(extra=[folded]))), [NOTETAKER])
    assert event.join_url == ZOOM and event.join_source == "DESCRIPTION"


# ─── unwrapping ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("wrap", "name"),
    [
        (safelinks, "safelinks"),
        (proofpoint_v1, "proofpoint_v1"),
        (proofpoint_v2, "proofpoint_v2"),
        (proofpoint_v3, "proofpoint_v3"),
        (google_redirect, "google"),
    ],
)
@pytest.mark.parametrize("url", [TEAMS_CLASSIC, TEAMS_MEET, ZOOM])
def test_each_wrapper_keeps_the_query(wrap, name, url):
    assert unwrap(wrap(url)) == (url, (name,))
    assert link_of(DESCRIPTION=f"Join: {wrap(url)}") == url


def test_double_wrapped_safe_links():
    twice = safelinks(safelinks(TEAMS_MEET))
    assert unwrap(twice) == (TEAMS_MEET, ("safelinks", "safelinks"))
    assert link_of(DESCRIPTION=twice) == TEAMS_MEET


def test_proofpoint_v3_known_vector():
    # four "%" characters in the base64 data ("JSUlJQ" = "%%%%")
    wrapped = (
        "https://urldefense.com/v3/__https://teams.microsoft.com/l/meetup-join/19*3ameeting_abc"
        "*40thread.v2/0?context=*7b*7d__;JSUlJQ!!CTRNKA9wMg0ARbw!abc$"
    )
    assert unwrap(wrapped) == (
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d",
        ("proofpoint_v3",),
    )
    # a run token "**A" takes two characters
    run = "https://urldefense.com/v3/__https://zoom.us/j/1?pwd**Aab__;PT0!!x$"
    assert unwrap(run) == ("https://zoom.us/j/1?pwd==ab", ("proofpoint_v3",))


def test_wrapper_limit_and_broken_wrappers():
    url = MEET
    for _ in range(3):
        url = safelinks(url)
    assert unwrap(url) == (MEET, ("safelinks",) * 3)
    assert unwrap(safelinks(url)) is None  # a fourth level is refused
    assert unwrap("https://nam12.safelinks.protection.outlook.com/?data=x") is None
    assert unwrap("https://urldefense.com/v3/__https://zoom.us/j/1*__;!!x$") is None  # short data


def test_mimecast_is_no_link_and_the_search_moves_on():
    mimecast = "https://protect-eu.mimecast.com/s/AbCdEfGhIjKlMn?domain=teams.microsoft.com"
    assert unwrap(mimecast) is None
    assert link_of(DESCRIPTION=mimecast) is None
    assert link_of(DESCRIPTION=mimecast, LOCATION=ZOOM) == ZOOM
    assert link_of(DESCRIPTION="https://url.uk.m.mimecastprotect.com/s/xYz") is None


# ─── refused links ──────────────────────────────────────────────────────────────────────────────


def test_javascript_scheme_is_refused():
    assert accept("javascript:alert(1)//https://meet.google.com/abc-defg-hij") is None
    assert link_of(X_GOOGLE_CONFERENCE="javascript:alert(1)") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://meet.google.com/abc-defg-hij",
        "https://ana@meet.google.com/abc-defg-hij",
        "https://evil.test\\@meet.google.com/abc-defg-hij",
        "https://meet.google.com:8443/abc-defg-hij",
        "https://meet.google.com.evil.test/abc-defg-hij",
        "https://evilzoom.us/j/123",
        "https://zoom.us.evil.test/j/123",
        "https://teams.microsoft.com.evil.test/meet/1?p=2",
        "https://meеt.google.com/abc-defg-hij",  # Cyrillic "е"
        "https://zооm.us/j/123",  # Cyrillic "о"
        "https://xn--zm-zmca.us/j/123",
        "https://meet.google.com./abc-defg-hij",
        "https://example.test/j/123",
    ],
)
def test_refused(url):
    assert accept(url) is None
    assert link_of(DESCRIPTION=url, X_GOOGLE_CONFERENCE=url) is None


def test_white_space_and_control_characters_are_refused():
    for url in (
        "https://meet.google.com/abc defg",
        "https://meet.google.com/abc\x00",
        "https://zoom.us/j/1\n",
    ):
        assert accept(url) is None


def test_wrapped_lookalike_is_refused_after_unwrapping():
    assert link_of(DESCRIPTION=safelinks("https://meet.google.com.evil.test/abc-defg-hij")) is None
    assert link_of(DESCRIPTION=google_redirect("javascript:alert(1)")) is None


def test_accept_normalises_host_case_and_default_port():
    assert accept("https://MEET.Google.com:443/abc-defg-hij") == (MEET, "google_meet")


def test_urls_in_decodes_entities_and_caps_the_scan():
    found = urls_in("a &lt;https://zoom.us/j/1?pwd=a&amp;b=c&gt; b")
    assert found == ["https://zoom.us/j/1?pwd=a&b=c"]
    assert len(urls_in(" ".join(f"https://x.test/{i}" for i in range(500)))) == 200
