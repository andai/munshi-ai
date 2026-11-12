# andai_core/dpdp

Data export: a person's copy of their data (OPS-02, DPDP access request; D-61). The operator's guide
is the "Copies of a person's data" section of `andai/docs/erasure.md`; this note is for whoever
changes the code.

| Module | What it does |
|---|---|
| `bundle.py` | The bundle format: pydantic models of `manifest.json`, `account.json` and `meetings/<id>.json`, with unknown fields forbidden. `schema()` is the published contract `andai/services/core/contracts/export-bundle.v1`; `tests/test_export_bundle.py` fails when the committed schema differs. |
| `builder.py` | What andai-worker runs for a claimed request (`andai_worker/exports.py`): read the person's account and the meetings they own (the erasure registry's `purge_meeting` rule), stream each recording once for size and SHA-256, seal the gzip-compressed JSON part (AAD `export:<org>:<request>`) into `export_requests`, mark it `ready` and audit `user.export.ready`. `too_large` over `ANDAI_EXPORT_MAX_BYTES`, `not_a_member` when the person left; other errors retry and fail `internal_error` after 5 attempts. `open_part` reads a stored part back. |
| `routes.py` | `POST/GET /api/v1/me/exports`, `POST /api/v1/org/users/{id}/exports`, `GET /api/v1/org/exports` and `GET /api/v1/exports/{id}/download`. The download writes the zip into a spooled temporary file, streaming each recording from the platform and comparing it with the manifest, then sends it. |

Rules for changes:

- The bundle holds the same set erasure removes. A new table of a person's data gets a rule in
  `erasure/registry.py` and, when it belongs in the copy, a field in `bundle.py`: a new optional
  field re-seals the contract (`pnpm seal:contracts`); renaming or removing one is `export-bundle.v2`.
- No secret goes into a bundle (password hashes, two-factor secrets, recovery codes, session
  tokens, platform keys) and no other person's address: other people appear by display name only.
- Recordings are never copied into the andai database; the build and the download both stream them
  with the org's platform key, holding at most one chunk in memory.
- `export_requests.bundle_ciphertext` is listed in `sealed.py`, so a key rotation re-wraps it.
- Logs carry request ids, sizes and error classes, never content, names or addresses.
