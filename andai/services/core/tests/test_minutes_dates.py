"""Due phrases in English, Hindi and Hinglish resolve against the meeting date (MOM-02); the rules
are in `andai_core/minutes/dates.py`. Meeting dates used:

* MON = Monday 2026-11-02
* WED = Wednesday 2026-12-30 (week, month and year roll over)
* FRI = Friday 2027-02-26 (February, not a leap year)
"""

from __future__ import annotations

from datetime import date

import pytest

from andai_core.minutes.dates import resolve

MON = date(2026, 11, 2)
WED = date(2026, 12, 30)
FRI = date(2027, 2, 26)

CASES = [
    # day words
    ("kal", MON, "2026-11-03"),
    ("kal tak", MON, "2026-11-03"),
    ("कल तक", MON, "2026-11-03"),
    ("tomorrow", WED, "2026-12-31"),
    ("kal", WED, "2026-12-31"),
    ("parso", WED, "2027-01-01"),
    ("परसों", MON, "2026-11-04"),
    ("day after tomorrow", FRI, "2027-02-28"),
    ("narso", MON, "2026-11-05"),
    ("aaj shaam tak", MON, "2026-11-02"),
    ("by EOD", MON, "2026-11-02"),
    ("end of day", FRI, "2027-02-26"),
    ("आज", WED, "2026-12-30"),
    # weekdays
    ("Monday tak", MON, "2026-11-09"),
    ("by Friday", MON, "2026-11-06"),
    ("this Friday", WED, "2027-01-01"),
    ("shukravar tak", MON, "2026-11-06"),
    ("शुक्रवार तक", MON, "2026-11-06"),
    ("next Friday", MON, "2026-11-13"),
    ("agle somvar", MON, "2026-11-09"),
    ("अगले बुधवार", WED, "2027-01-06"),
    ("next week Tuesday", FRI, "2027-03-02"),
    ("Friday EOD", FRI, "2027-03-05"),
    # counts
    ("तीन दिन में", MON, "2026-11-05"),
    ("3 din mein", WED, "2027-01-02"),
    ("in two weeks", MON, "2026-11-16"),
    ("do hafte mein", WED, "2027-01-13"),
    ("ek mahine mein", FRI, "2027-03-26"),
    ("within 10 days", FRI, "2027-03-08"),
    ("२ दिन में", MON, "2026-11-04"),
    # weeks and months
    ("agle hafte", MON, "2026-11-09"),
    ("next week", WED, "2027-01-04"),
    ("अगले हफ्ते", FRI, "2027-03-01"),
    ("end of next week", MON, "2026-11-13"),
    ("is hafte", MON, "2026-11-06"),
    ("end of the week", FRI, "2027-02-26"),
    ("hafte ke end tak", WED, "2027-01-01"),
    ("next month", WED, "2027-01-01"),
    ("agle mahine", MON, "2026-12-01"),
    ("month end", FRI, "2027-02-28"),
    ("महीने के अंत तक", MON, "2026-11-30"),
    ("end of next month", WED, "2027-01-31"),
    # written dates
    ("31 October", MON, "2027-10-31"),
    ("20 November", MON, "2026-11-20"),
    ("Nov 20th", MON, "2026-11-20"),
    ("5 January", WED, "2027-01-05"),
    ("20 नवंबर", MON, "2026-11-20"),
    ("20/11", MON, "2026-11-20"),
    ("15-01-2027", MON, "2027-01-15"),
    ("2026-12-15", MON, "2026-12-15"),
    ("20.11.2026", MON, "2026-11-20"),
    ("31 December 2026", MON, "2026-12-31"),
]

UNKNOWN = [
    None,
    "",
    "   ",
    "jaldi",
    "ASAP",
    "Diwali ke baad",
    "soon",
    "when possible",
    "5.30 tak",  # a time, not a date
    "mai 5 baje karunga",  # "mai" is "I", 5 is a time
    "30 February",
]


# Tuesday 2026-10-13, the date of the gold meeting where "दो-तीन दिन पहले" was read as +3.
TUE = date(2026, 10, 13)

# A count of days, weeks or months before something: the anchor's date minus the count when the
# phrase states the anchor's date, never the meeting date plus the count.
BEFORE = [
    ("20 अक्टूबर के शिविर से दो दिन पहले", TUE, "2026-10-18"),
    ("two days before 20 October", TUE, "2026-10-18"),
    ("2-3 days before 20 October", TUE, "2026-10-17"),  # a range: the larger count
    ("दो-तीन दिन पहले 20 अक्टूबर", TUE, "2026-10-17"),
    ("do din pehle shukravar se", TUE, "2026-10-14"),  # Friday 2026-10-16 minus 2
    ("1 hafte pehle 30 November", TUE, "2026-11-23"),
    ("a week prior to 30 November", TUE, "2026-11-23"),
    ("ek mahine pahle 20 December", TUE, "2026-11-20"),
    ("3 days before 2026-10-20", TUE, "2026-10-17"),
]

BEFORE_UNKNOWN = [
    ("शिविर से दो-तीन दिन पहले", TUE),  # no anchor date said
    ("camp se do din pehle", TUE),
    ("2 days before the camp", TUE),
    ("2-3 days before the launch", TUE),
    ("5 days before 14 October", TUE),  # 9 October: already past on the meeting date
]

# "pehle" / "before" without a counted unit right before it is not this rule: the existing rules
# decide as they did.
BEFORE_FALLS_THROUGH = [
    ("Friday se pehle", TUE, "2026-10-16"),
    ("20 November se pehle", TUE, "2026-11-20"),
    ("before Friday", TUE, "2026-10-16"),
    ("kal se pehle", TUE, "2026-10-14"),
]


@pytest.mark.parametrize(("phrase", "meeting", "expected"), BEFORE)
def test_a_count_before_a_stated_anchor_is_the_anchor_minus_the_count(
    phrase: str, meeting: date, expected: str
) -> None:
    assert resolve(phrase, meeting) == date.fromisoformat(expected)


@pytest.mark.parametrize(("phrase", "meeting"), BEFORE_UNKNOWN)
def test_a_count_before_with_no_anchor_or_a_past_result_has_no_date(
    phrase: str, meeting: date
) -> None:
    assert resolve(phrase, meeting) is None


@pytest.mark.parametrize(("phrase", "meeting", "expected"), BEFORE_FALLS_THROUGH)
def test_before_without_a_count_keeps_the_existing_rules(
    phrase: str, meeting: date, expected: str
) -> None:
    assert resolve(phrase, meeting) == date.fromisoformat(expected)


def test_pehle_without_a_count_or_a_date_has_no_date() -> None:
    assert resolve("pehle ye karo", TUE) is None
    assert resolve("पहले", TUE) is None


@pytest.mark.parametrize(("phrase", "meeting", "expected"), CASES)
def test_a_due_phrase_resolves_to_one_date(phrase: str, meeting: date, expected: str) -> None:
    assert resolve(phrase, meeting) == date.fromisoformat(expected)


@pytest.mark.parametrize("phrase", UNKNOWN)
def test_a_phrase_no_rule_covers_has_no_date(phrase) -> None:
    assert resolve(phrase, MON) is None


def test_the_table_is_large_and_mixes_scripts() -> None:
    assert len(CASES) >= 30
    assert any(any("ऀ" <= c <= "ॿ" for c in phrase) for phrase, _, _ in CASES)


def test_resolution_is_deterministic() -> None:
    for phrase, meeting, _ in CASES:
        assert resolve(phrase, meeting) == resolve(phrase, meeting)
