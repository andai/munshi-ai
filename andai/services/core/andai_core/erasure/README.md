# andai_core/erasure

User erasure (OPS-02; decisions D-59, D-61, D-63). The operator's guide is
`andai/docs/erasure.md`; this note is for whoever changes the code.

| Module | What it does |
|---|---|
| `registry.py` | `ERASURE_RULES`: one rule per column that refers to a person (every FK to `users.id` and every column named like a person), with its action (`delete`, `tombstone`, `purge_meeting`, `account`, `keep`) and scope (`org`, `account`). `tests/test_erasure_registry.py` fails when a model gains such a column without a rule. |
| `requests.py` | `POST /api/v1/org/users/{id}/erasure` and `GET /api/v1/org/erasures`; `create_request`, `remove_from_org`, `memberships_of` and `lock_subject`, shared with the operator CLI (`python -m andai_core erase-user`). |
| `runner.py` | The step machine andai-worker drives (`andai_worker/erasure.py`): `stop_live` → `delete_owned` → `wait_purge` → `scrub` → `account` → `done`. `SCRUB_HANDLERS` and `ACCOUNT_HANDLERS` act on the registry's rules; the registry test checks that every acting rule has a handler. |

Rules for changes:

- A new column that refers to a person needs a rule in `registry.py`, and a handler in
  `runner.py` when its action is `delete` or `tombstone` (scope `org`) or it is an `account`
  rule. Phases 8 and 9 add theirs here.
- Owned meetings are removed only through the meeting purge (`meetings/deletion.py`); never add a
  second platform deletion path.
- Nothing here stores or logs a name or an address: request rows, counts, metrics and log lines
  carry ids, states and numbers only.
- Every audit event of a request is written in its `audit_org_id`; the account step runs only for
  a request with `account` set, once no membership remains and no other request for the person is
  unfinished.
