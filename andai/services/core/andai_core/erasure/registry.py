"""Every column of the andai database that refers to a person, and what erasure does with it (D-59).

`ERASURE_RULES` holds one `ErasureRule` per user-referencing column: every foreign key to
`users.id` and every column whose name says it holds a person (`PERSON_COLUMN`). `uncovered()`
lists the columns of a `MetaData` that have no rule; `tests/test_erasure_registry.py` runs it over
`andai_core.db.models.Base.metadata`, so a later phase that adds such a column without a rule here
fails the suite.

Actions:

* `delete` — the row goes (in the org being erased for `scope="org"`, everywhere for
  `scope="account"`).
* `tombstone` — the column is re-pointed at `TOMBSTONE_USER_ID` ("Erased user"); the row stays
  because it belongs to someone else's record (a share they granted on another person's meeting).
* `purge_meeting` — the person's own meetings are removed through the meeting purge path
  (`meetings.deletion`), platform first.
* `account` — the account step anonymises the users row in place once no membership remains.
* `keep` — kept on purpose; `reason` says why.

`scope` is where the runner applies the rule: `org` in the scrub of the org the request is for,
`account` in the account step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import MetaData

Action = Literal["delete", "tombstone", "purge_meeting", "account", "keep"]
Scope = Literal["org", "account"]

ACTIONS: tuple[str, ...] = ("delete", "tombstone", "purge_meeting", "account", "keep")
SCOPES: tuple[str, ...] = ("org", "account")

# Column names that hold a person (an id, an address, a name or what they said).
PERSON_COLUMN = re.compile(
    r"^(user_id|.*_user_id|.*_by|email|sender|mail_from|rcpt_to|grantee_id|display_name|speaker"
    r"|attendees)$"
)


@dataclass(frozen=True)
class ErasureRule:
    table: str
    column: str
    action: Action
    scope: Scope
    reason: str


ERASURE_RULES: tuple[ErasureRule, ...] = (
    # ── the org's own records of the person ──
    ErasureRule("org_memberships", "user_id", "delete", "org",
                "the membership ends in the request's transaction"),
    ErasureRule("team_memberships", "user_id", "delete", "org",
                "team memberships in the org end in the request's transaction"),
    ErasureRule("sessions", "user_id", "delete", "org",
                "sessions in the org end at the request; the account step ends any others"),
    ErasureRule("meetings", "requested_by", "purge_meeting", "org",
                "the person's own meetings go through the meeting purge, platform first"),
    ErasureRule("meetings", "deleted_by", "tombstone", "org",
                "who deleted another person's meeting"),
    ErasureRule("meetings", "skipped_by", "tombstone", "org",
                "who skipped another person's scheduled meeting"),
    ErasureRule("meeting_shares", "grantee_id", "delete", "org",
                "shares the person received (grantee_type user)"),
    ErasureRule("meeting_shares", "granted_by", "tombstone", "org",
                "a share the person granted on another person's meeting stays with that meeting"),
    ErasureRule("invites", "invited_by", "tombstone", "org", "who sent an invite"),
    ErasureRule("invites", "email", "delete", "org", "invites to the person's address"),
    ErasureRule("invite_events", "requested_by", "delete", "org",
                "calendar invites the person sent the notetaker"),
    ErasureRule("inbound_mail", "resolved_user_id", "delete", "org",
                "mail the notetaker attributed to the person (andai_erase_inbound)"),
    ErasureRule("inbound_mail", "sender", "delete", "org",
                "mail sent from the person's address (andai_erase_inbound; org-less rows in the"
                " account step)"),
    ErasureRule("inbound_mail", "mail_from", "delete", "org",
                "mail whose envelope sender is the person's address (andai_erase_inbound)"),
    ErasureRule("bot_settings", "updated_by", "tombstone", "org",
                "who last changed the notetaker's auto-join"),
    ErasureRule("export_requests", "subject_user_id", "delete", "org",
                "copies of the person's data (DPDP export) and their sealed bundles"),
    ErasureRule("export_requests", "requested_by", "tombstone", "org",
                "an export the person asked for, as an admin, of another member's data"),
    # ── kept on purpose ──
    ErasureRule("inbound_mail", "rcpt_to", "keep", "org",
                "the notetaker mailbox's own addresses; rows of the person go by sender above"),
    ErasureRule("meetings", "attendees", "keep", "org",
                "names in other people's meetings stay (D-61); the person's own meetings are"
                " purged"),
    ErasureRule("transcript_segments", "speaker", "keep", "org",
                "speech in other people's meetings stays (D-61); the person's own meetings are"
                " purged"),
    ErasureRule("consent_records", "removed_by", "keep", "org",
                "who removed the bot from a meeting as the platform reported it; other people's"
                " meetings (D-61)"),
    ErasureRule("platform_principals", "platform_user_id", "keep", "org",
                "the org's account on the meeting platform, not a person"),
    ErasureRule("audit_events", "actor_user_id", "keep", "org",
                "the trail keeps the user id (D-63); the name it resolves to is 'Erased user'"),
    ErasureRule("erasure_requests", "subject_user_id", "keep", "org",
                "the erasure's own record: an id, never a name or address"),
    ErasureRule("erasure_requests", "requested_by", "keep", "org",
                "the erasure's own record: who asked, as an id"),
    # ── the account ──
    ErasureRule("users", "email", "account", "account",
                "anonymised to erased-<request id>@munshi.invalid"),
    ErasureRule("users", "display_name", "account", "account", "anonymised to 'Erased user'"),
    ErasureRule("user_totp", "user_id", "delete", "account", "the two-factor secret"),
    ErasureRule("recovery_codes", "user_id", "delete", "account", "two-factor recovery codes"),
    ErasureRule("password_resets", "user_id", "delete", "account", "password reset links"),
)


def rules_by_column() -> dict[tuple[str, str], ErasureRule]:
    return {(rule.table, rule.column): rule for rule in ERASURE_RULES}


def referencing_columns(metadata: MetaData) -> set[tuple[str, str]]:
    """Every (table, column) of `metadata` with a foreign key to `users.id` or a person's name."""
    found: set[tuple[str, str]] = set()
    for table in metadata.tables.values():
        for column in table.columns:
            to_users = any(
                fk.column.table.name == "users" and fk.column.name == "id"
                for fk in column.foreign_keys
            )
            if to_users or PERSON_COLUMN.match(column.name):
                found.add((table.name, column.name))
    return found


def uncovered(metadata: MetaData) -> list[tuple[str, str]]:
    """The user-referencing columns of `metadata` that no rule covers, sorted."""
    covered = set(rules_by_column())
    return sorted(referencing_columns(metadata) - covered)


def rules(scope: str, *actions: str) -> list[ErasureRule]:
    return [r for r in ERASURE_RULES if r.scope == scope and (not actions or r.action in actions)]


__all__ = [
    "ACTIONS",
    "ERASURE_RULES",
    "PERSON_COLUMN",
    "SCOPES",
    "ErasureRule",
    "referencing_columns",
    "rules",
    "rules_by_column",
    "uncovered",
]
