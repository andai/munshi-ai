"""Invite parsing for the notetaker mailbox: stored bytes in, `ParsedEvent`s out (see README.md).

Pure and offline: no database, no network, no scheduling decision.
"""

from andai_intake.invite.ics import (
    Attendee,
    InviteParseError,
    Method,
    ParsedEvent,
    normalise_uid,
    parse,
)

__all__ = ["Attendee", "InviteParseError", "Method", "ParsedEvent", "normalise_uid", "parse"]
