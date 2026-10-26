# recordings — chunk upload + finalize → `meeting.data` JSONB

Ported from the parent `recordings.internal_upload_recording` + `recording_finalizer` +
`recording_jsonb`. The bot streams recording chunks (authenticated by the MeetingToken it carries);
each chunk lands in object storage and is folded into the recording's JSONB payload under
`meeting.data['recordings']` — there is **NO separate recordings table**. Finalize concatenates a
recording's chunks into a master via the golden-locked `build_recording_master` codec (recording.v1)
and stamps the JSONB media-file.

## Front door
- `build_router(repo, storage)` — the mountable routes (the unified app mounts them): POST
  `/internal/recordings/upload`, GET `/recordings`, GET `/recordings/{id}/master`.
- `upload_chunk(...)` / `finalize_master(...)` — the flow core (callable directly in tests).
- `apply_chunk_to_recording` / `chunk_storage_key` / `master_storage_key` /
  `new_recording_numeric_id` — the pure JSONB record materializers (no IO/DB).
- `Storage` / `RecordingRepo` ports + `SessionNotFound`.
- `adapters.build_production_router(...)` — wire with real MinIO/S3 + SQLAlchemy.
- `fakes` — `InMemoryStorage` / `InMemoryRecordingRepo` (offline drivers).

## The JSONB shape
`meeting.data['recordings']` is a list of recording dicts (`id`, `session_uid`, `source="bot"`,
`status`, `media_files[]`). Each `media_files[]` entry tracks per-type cumulative
`file_size_bytes` / `chunk_count`, the chunk/master `storage_path`, and `is_final` / `finalized_by`
(Pack U.7 master-preserve + sticky-COMPLETED status are ported verbatim).

## Object keys and the storage prefix
A meeting may carry `meeting.data.storage_prefix` (`org/<uuid>`). The spawn copies it from the
owner's identity record (admin-api `/internal/users/{id}/bot-context`); it never comes from a
request, and it is fixed for the meeting's lifetime. Every object written for the meeting is keyed
under it:

| Object | Without a prefix | With a prefix |
|---|---|---|
| Recording chunk | `recordings/{user}/{rec}/{session}/{type}/{seq:06d}.{fmt}` | `{prefix}/recordings/…` |
| Master | `recordings/{user}/{rec}/{session}/{type}/master.{fmt}` | `{prefix}/recordings/…` |
| Signal tape part | `signal/{user}/{meeting}/{session}/{part}.{fmt}` | `{prefix}/signal/…` |

The key builders (`chunk_storage_key`, `signal_tape_prefix`, `signal_tape_key`) take the prefix
and raise `InvalidStoragePrefix` for any value not shaped `org/<uuid>`. Upload and tape writes read
it with `RecordingRepo.storage_prefix_of(meeting_id)`; deletion receives it on the recording dicts
the repository hands over (`prepare_recording_deletion`, and the collector's
`recordings_for_artifact_deletion`) and lists only that keyspace. The tape janitor sweeps the
top-level `signal/` root and each `org/<uuid>/signal/` root under one budget.

## P3 seams (NOT built here)
The raw byte-stream / Range download of a finalized master, and the lifecycle-driven server-side
finalize (this carve finalizes lazily on read via `GET /recordings/{id}/master`).

Tests: `../../../tests/test_recordings.py`. Codec golden: `../../../tests/test_recording_golden.py`.
