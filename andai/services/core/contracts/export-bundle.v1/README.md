# export-bundle.v1 — a person's copy of their data

The format of the zip a data export produces (OPS-02, DPDP access request; D-61). The JSON files of
the bundle conform to `export-bundle.schema.json`, which is generated from the pydantic models in
`andai/services/core/andai_core/dpdp/bundle.py`
(`uv run --directory andai python -m andai_core.dpdp.bundle --schema`); `test_export_bundle.py`
fails when the two differ.

| File in the zip | `$defs` | What it holds |
|---|---|---|
| `manifest.json` | `Manifest` | `schema` (`munshi.export-bundle/v1`), `generatedAt`, `product`, `version`, the subject's user id, the org, every other file with `bytes`, `sha256` and `mediaType`, and `recordingsUnavailable` (meeting id and reason: `no_master`, `too_large`, `unsupported_media`, `meeting_in_progress`) |
| `account.json` | `Account` | the account (id, email, display name, created, last sign-in, two-factor on or off), the membership of the org (role, joined, teams by name) and the operator's processing note |
| `meetings/<meeting id>.json` | `MeetingExport` | one per meeting the person owns: the meeting record, attendees and share grantees by name, the consent record, every transcript segment (`textRaw` as transcribed, `textCanonical` in Roman script), every minutes version, and the recordings' file names with size and SHA-256 |
| `recordings/<meeting id>/<recording id>-<type>.<ext>` | — | the recordings (`audio/wav`, `audio/webm`, `video/webm`) |

Every model forbids unknown fields: a bundle never holds password hashes, two-factor secrets,
recovery codes, session tokens, platform keys or other people's email addresses. Other people appear
by display name only. The set of meetings is the set erasure removes (`erasure/registry.py`,
`meetings.requested_by`).

`golden/` holds synthetic examples (prefix → shape: `manifest-*`, `account-*`, `meeting-*`);
`node validate.mjs --check` validates them (`gate:schema`). The contract is sealed in
`contracts.seal.json` (`gate:contract-version`): a back-compatible change (a new optional field)
re-seals with `pnpm seal:contracts`; a breaking one adds `export-bundle.v2`.
