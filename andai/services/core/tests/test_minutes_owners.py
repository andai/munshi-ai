"""The owner rule (MOM-02, D-49): a named owner stays only when the model's confidence is at least
0.6 and the name matches exactly one attendee (fold-normalised); otherwise `unassigned`."""

from __future__ import annotations

import pytest

from andai_core.minutes.owners import UNASSIGNED, resolve

ATTENDEES = [
    {"name": "Priya Sharma", "source": "invite"},
    {"name": "Rahul", "source": "speaker"},
    {"name": "अमित कुमार", "source": "speaker"},
    {"name": "Deepak Verma", "source": "invite"},
    {"name": "Deepak Singh", "source": "invite"},
]


@pytest.mark.parametrize(
    ("owner", "confidence", "expected"),
    [
        # matched, confident
        ("Priya Sharma", 0.9, "Priya Sharma"),
        ("priya sharma", 0.6, "Priya Sharma"),
        ("Priya", 0.8, "Priya Sharma"),
        ("प्रिया", 0.8, "Priya Sharma"),
        ("प्रिया शर्मा", 0.95, "Priya Sharma"),
        ("Priya ji", 0.9, "Priya Sharma"),
        ("Rahul", 0.7, "Rahul"),
        ("राहुल", 0.7, "Rahul"),
        ("Rahul Mehta", 0.9, "Rahul"),  # the attendee is listed by first name only
        ("Amit", 0.9, "अमित कुमार"),
        ("Amit Kumar", 0.9, "अमित कुमार"),
        ("Deepak Verma", 0.9, "Deepak Verma"),
        # not confident enough
        ("Priya Sharma", 0.59, UNASSIGNED),
        ("Rahul", 0.0, UNASSIGNED),
        ("Rahul", None, UNASSIGNED),
        # not an attendee, ambiguous, or not a person
        ("Sunita", 0.99, UNASSIGNED),
        ("Deepak", 0.99, UNASSIGNED),  # two attendees named Deepak
        ("Sharma", 0.9, "Priya Sharma"),
        ("team", 0.99, UNASSIGNED),
        ("The team", 0.99, UNASSIGNED),
        ("we", 0.99, UNASSIGNED),
        ("hum log", 0.99, UNASSIGNED),
        ("सब", 0.99, UNASSIGNED),
        ("everyone", 0.99, UNASSIGNED),
        ("Priya and Rahul", 0.99, UNASSIGNED),
        ("Priya aur Rahul", 0.99, UNASSIGNED),
        ("Priya, Rahul", 0.99, UNASSIGNED),
        ("Priya/Rahul", 0.99, UNASSIGNED),
        ("", 0.99, UNASSIGNED),
        (None, 0.99, UNASSIGNED),
        ("unassigned", 0.99, UNASSIGNED),
    ],
)
def test_owner_matrix(owner, confidence, expected) -> None:
    assert resolve(owner, confidence, ATTENDEES) == expected


def test_no_attendees_means_no_owner() -> None:
    assert resolve("Priya", 0.99, []) == UNASSIGNED


def test_attendees_may_be_plain_names() -> None:
    assert resolve("priya", 0.9, ["Priya Sharma", "Rahul"]) == "Priya Sharma"


def test_unknown_speaker_is_never_an_owner() -> None:
    assert resolve("Unknown speaker", 0.99, [{"name": "Unknown speaker"}]) == UNASSIGNED
