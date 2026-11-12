"""The data export bundle: what a person's copy of their data holds (contract `export-bundle.v1`).

A bundle is a zip of:

* `manifest.json` — `Manifest`: the format (`schema`), when and by which version it was made, whose
  data it is (`subject`) and in which org, every other file with its size and SHA-256, and the
  recordings that could not be included (`recordingsUnavailable`).
* `account.json` — `Account`: the person's account and their membership of the org.
* `meetings/<meeting id>.json` — `MeetingExport`: one per meeting the person owns (the set erasure
  removes, `erasure.registry`): its record, who it was shared with (by name), the consent record,
  every transcript segment and every minutes version, and the recordings' file names and checksums.
* `recordings/<meeting id>/<recording id>-<type>.<ext>` — the recordings themselves.

The models forbid unknown fields, so a field that is not listed here (a password hash, a two-factor
secret, another person's address) cannot be written into a bundle. People other than the subject
appear by display name only.

`schema()` is the JSON Schema (draft 2020-12, one `$defs` entry per file) committed as
`contracts/export-bundle.v1/export-bundle.schema.json`; `tests/test_export_bundle.py` fails when
the two differ. `python -m andai_core.dpdp.bundle --schema` prints it.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic.json_schema import models_json_schema

SCHEMA = "munshi.export-bundle/v1"
SCHEMA_ID = "https://munshi.ai/contracts/export-bundle.v1/export-bundle.schema.json"
PRODUCT = "munshi.ai"
SHA256 = r"^[0-9a-f]{64}$"

UNAVAILABLE_REASONS = ("no_master", "too_large", "unsupported_media", "meeting_in_progress")


class _Part(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True, frozen=True
    )


# ─── manifest.json ────────────────────────────────────────────────────────────────────────────────


class Subject(_Part):
    """Whose data the bundle holds."""

    user_id: uuid.UUID


class OrgRef(_Part):
    """The org the copy was made in."""

    org_id: uuid.UUID
    name: str


class ManifestFile(_Part):
    """One file of the bundle other than the manifest."""

    path: str = Field(min_length=1, max_length=512)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256)
    media_type: str = Field(min_length=1)


class RecordingUnavailable(_Part):
    """A recording of an owned meeting that is not in the bundle, and why: the platform has no
    finished master of it (`no_master`), it is larger than a bundle takes (`too_large`), its
    media type is not one the bundle names (`unsupported_media`), or the meeting was still going on
    when the copy was made (`meeting_in_progress`: its recording is still growing)."""

    meeting_id: uuid.UUID
    reason: Literal["no_master", "too_large", "unsupported_media", "meeting_in_progress"]


class Manifest(_Part):
    """`manifest.json`."""

    schema_: Literal["munshi.export-bundle/v1"] = Field(alias="schema")
    generated_at: datetime
    product: str
    version: str
    subject: Subject
    org: OrgRef
    files: list[ManifestFile]
    recordings_unavailable: list[RecordingUnavailable]


# ─── account.json ─────────────────────────────────────────────────────────────────────────────────


class TeamRef(_Part):
    team_id: uuid.UUID
    name: str
    lead: bool


class Membership(_Part):
    role: Literal["owner", "admin", "member"]
    joined_at: datetime
    teams: list[TeamRef]


class Processing(_Part):
    """What the operator tells the person about how their data is processed (DPDP s.11)."""

    note: str


class Account(_Part):
    """`account.json`."""

    user_id: uuid.UUID
    email: str
    display_name: str
    created_at: datetime
    last_login_at: datetime | None
    two_factor_enabled: bool
    membership: Membership
    processing: Processing


# ─── meetings/<id>.json ───────────────────────────────────────────────────────────────────────────


class ShareGrant(_Part):
    """Who the meeting was shared with: a person's or a team's name (null for a team that is
    gone)."""

    type: Literal["user", "team"]
    name: str | None


class Consent(_Part):
    """How the room was told about the recording (the meeting's consent record)."""

    bot_display_name: str
    disclosure_text: str
    privacy_notice_url: str
    disclosure_channel: str
    disclosure_detail: str | None
    disclosure_posted_at: datetime | None
    admitted_at: datetime
    removed_at: datetime | None
    removed_by: str | None


class Segment(_Part):
    """One transcript segment: the platform's text (`textRaw`) and its Roman form
    (`textCanonical`); `start` and `end` are seconds from the start of the recording."""

    id: str
    speaker: str
    start: float | None
    end: float | None
    absolute_start: datetime | None
    text_raw: str
    text_canonical: str
    language: str | None


class MinutesVersion(_Part):
    """One version of the minutes; `body` is the minutes document as stored."""

    version: int = Field(ge=1)
    template_id: str
    output_language: Literal["en", "hi", "hinglish"]
    created_at: datetime
    body: dict[str, Any]


class RecordingFile(_Part):
    """A recording in the bundle: its path in the zip, size and SHA-256."""

    file: str = Field(min_length=1, max_length=512)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256)
    media_type: str = Field(min_length=1)


class MeetingExport(_Part):
    """`meetings/<meeting id>.json`."""

    id: uuid.UUID
    title: str | None
    platform: str
    source: str
    language: str | None
    status: str
    failure_reason: str | None
    created_at: datetime
    scheduled_start: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    attendees: list[str]
    shared_with: list[ShareGrant]
    consent: Consent | None
    transcript: list[Segment]
    minutes: list[MinutesVersion]
    recordings: list[RecordingFile]


FILES: tuple[tuple[str, type[_Part]], ...] = (
    ("manifest", Manifest),
    ("account", Account),
    ("meeting", MeetingExport),
)


def dump(part: _Part) -> bytes:
    """A file's bytes: the model as JSON with the published (camelCase) names, UTF-8, indented."""
    return (
        json.dumps(part.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
        + "\n"
    ).encode()


def schema() -> dict:
    """The bundle's JSON Schema: one `$defs` entry per file kind, with their parts."""
    _, defs = models_json_schema(
        [(model, "serialization") for _, model in FILES],
        by_alias=True,
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "munshi data export bundle (export-bundle.v1)",
        "description": (
            "The JSON files of a munshi data export: manifest.json (Manifest), account.json"
            " (Account) and meetings/<id>.json (MeetingExport). Generated from"
            " andai_core/dpdp/bundle.py."
        ),
        "$defs": dict(sorted(defs["$defs"].items())),
    }


def schema_text() -> str:
    return json.dumps(schema(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andai_core.dpdp.bundle")
    parser.add_argument("--schema", action="store_true", help="print the JSON Schema")
    args = parser.parse_args(argv)
    if args.schema:
        print(schema_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PRODUCT",
    "SCHEMA",
    "Account",
    "Consent",
    "Manifest",
    "ManifestFile",
    "MeetingExport",
    "Membership",
    "MinutesVersion",
    "OrgRef",
    "Processing",
    "RecordingFile",
    "RecordingUnavailable",
    "Segment",
    "ShareGrant",
    "Subject",
    "TeamRef",
    "dump",
    "schema",
]
