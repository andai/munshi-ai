"""The erasure registry (D-59) covers every user-referencing column of the andai database exactly
once, names only columns that exist, says why a column is kept, and fails when a model adds a new
reference to a person without a rule."""

from __future__ import annotations

import uuid

from sqlalchemy import Column, ForeignKey, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import UUID

from andai_core.db.models import Base
from andai_core.erasure import registry, runner
from andai_core.erasure.registry import (
    ACTIONS,
    ERASURE_RULES,
    SCOPES,
    referencing_columns,
    uncovered,
)


def test_every_user_reference_has_exactly_one_rule() -> None:
    keys = [(rule.table, rule.column) for rule in ERASURE_RULES]
    assert len(keys) == len(set(keys)), "a column has two rules"
    assert uncovered(Base.metadata) == []
    assert set(keys) == referencing_columns(Base.metadata)


def test_every_foreign_key_to_users_is_covered() -> None:
    fks = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        for fk in column.foreign_keys
        if fk.column.table.name == "users"
    }
    assert fks == {
        ("org_memberships", "user_id"),
        ("sessions", "user_id"),
        ("user_totp", "user_id"),
        ("recovery_codes", "user_id"),
        ("password_resets", "user_id"),
        ("invites", "invited_by"),
        ("meetings", "requested_by"),
        ("meetings", "deleted_by"),
        ("meetings", "skipped_by"),
        ("invite_events", "requested_by"),
        ("meeting_shares", "granted_by"),
        ("inbound_mail", "resolved_user_id"),
        ("bot_settings", "updated_by"),
        ("export_requests", "subject_user_id"),
        ("export_requests", "requested_by"),
    }
    assert fks <= set(registry.rules_by_column())


def test_every_rule_names_an_existing_column_with_a_known_action() -> None:
    for rule in ERASURE_RULES:
        table = Base.metadata.tables.get(rule.table)
        assert table is not None, rule
        assert rule.column in table.columns, rule
        assert rule.action in ACTIONS, rule
        assert rule.scope in SCOPES, rule
        assert rule.reason.strip(), rule


def test_kept_columns_say_why() -> None:
    kept = {(r.table, r.column): r.reason for r in ERASURE_RULES if r.action == "keep"}
    assert ("audit_events", "actor_user_id") in kept
    assert ("transcript_segments", "speaker") in kept
    assert ("meetings", "attendees") in kept
    assert ("erasure_requests", "subject_user_id") in kept
    assert all(len(reason) >= 20 for reason in kept.values())


def test_the_scrub_handles_every_org_rule_it_must_act_on() -> None:
    acting = {
        (r.table, r.column)
        for r in ERASURE_RULES
        if r.scope == "org" and r.action in ("delete", "tombstone")
    }
    assert acting == set(runner.SCRUB_HANDLERS)
    account = {
        (r.table, r.column) for r in ERASURE_RULES if r.scope == "account"
    }
    assert account == set(runner.ACCOUNT_HANDLERS)


def _with(extra: Table | None = None, *, column: Column | None = None) -> MetaData:
    """A copy of the andai metadata, plus one synthetic table or one column on `meetings`."""
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        copied = table.to_metadata(metadata)
        if column is not None and copied.name == "meetings":
            copied.append_column(column)
    if extra is not None:
        extra.to_metadata(metadata)
    return metadata


def test_a_new_foreign_key_to_users_without_a_rule_fails() -> None:
    scratch = MetaData()
    Table("users", scratch, Column("id", UUID(as_uuid=True), primary_key=True))
    notes = Table(
        "meeting_notes",
        scratch,
        Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        Column("author", UUID(as_uuid=True), ForeignKey("users.id")),
    )
    assert uncovered(_with(notes)) == [("meeting_notes", "author")]


def test_a_new_person_column_without_a_rule_fails() -> None:
    assert uncovered(_with(column=Column("approved_by", Text))) == [("meetings", "approved_by")]
    assert uncovered(_with(column=Column("owner_user_id", UUID(as_uuid=True)))) == [
        ("meetings", "owner_user_id")
    ]
