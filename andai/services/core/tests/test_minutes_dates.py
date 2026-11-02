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
