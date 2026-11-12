# Erasing a person's data

munshi.ai erases a person's data when an organisation's admin or owner asks for it on the members
page, or when the operator asks for it across every organisation the person belongs to (OPS-02,
DPDP). This page says what goes, what stays, how long it takes, and what copies outlive it.

## Who can ask, and for what

| Who | Where | Scope |
|---|---|---|
| An org's owner or admin | `/admin/members` → a member's actions → **Erase data…** (`POST /api/v1/org/users/{id}/erasure`, typed-email confirmation) | That member's data **in this organisation**. Their account is anonymised too when they belong to no other organisation. |
| The operator | `make -C andai erase-user PROJECT=… EMAIL=… CONFIRM=…` (`python -m andai_core erase-user` in the andai-core container) | One request per organisation the person belongs to, and their account. |

Refused, with no override: erasing the organisation's **owner** (transfer ownership first; the
operator command refuses anyone who owns any organisation) and erasing **oneself** through the
portal. Members and team leads cannot ask.

A person who no longer belongs to any organisation (they left, or an earlier erasure removed them)
can still have their account erased by the operator: `AUDIT_ORG=<org id>` names an organisation they
belonged to (its audit trail names them, or an earlier erasure there did), and that organisation's
audit trail records the request.

## What happens, and when

At the request, in one transaction: the person's team memberships, their membership and their
sessions in the organisation end, and `user.erase.request` is audited. They lose access at once;
signing in answers exactly as for a disabled account.

andai-worker then works the request (every 10 s; `andai_core/erasure/runner.py`):

1. **Live meetings stop.** Scheduled meetings they sent the notetaker to are cancelled; a bot on its
   way in or in the room is stopped, and the request waits until every one of their meetings is
   over.
2. **Their meetings are deleted** exactly as the meeting's Delete button does (`meeting.delete` per
   meeting), and the meeting purge removes them: first on the platform (recording chunks and master
   under the meeting's storage prefix, then its transcript rows; the platform keeps a content-free
   meeting row), then in andai (transcript and its search text, minutes, shares, consent record, the
   meeting row; `meeting.purge`). A purge that fails 30 times fails the erasure (`purge_failed`).
3. **The organisation's other rows are scrubbed** (one transaction): shares they received, invites
   to their address, the calendar invites they sent the notetaker and the notetaker mail from their
   address go; where another person's record names them (who granted a share, who deleted or
   skipped someone's meeting, who sent an invite, who changed the auto-join setting) it is re-pointed
   at the **Erased user** tombstone.
4. **The account is anonymised** when the request carries it and no membership is left (and no
   other erasure of the person is unfinished): sessions, the two-factor secret, recovery codes and
   password reset links go, mail from their address that reached no organisation goes, and the
   account row keeps only its id: `erased-<request id>@munshi.invalid`, "Erased user", no password,
   status `erased`. The row itself stays because other organisations' records may still point at
   it; it no longer identifies anyone.
5. `user.erase.complete` is audited in the organisation's trail.

**SLA: every online copy within 24 hours of the request; in practice a few minutes** (the
local e2e stack took 19 to 25 seconds from the request to `done` for a member with one recorded
meeting). `munshi_erasure_oldest_pending_seconds` above 86400 means
the SLA is missed; `munshi_erasures_failed` above 0 means a request needs the operator
(`metrics.md`). `make -C andai erase-status PROJECT=… ID=<request id>` shows a request's state, step,
attempts, error and counts. A failed request does not retry on its own; its account step, and the
account steps of the person's other requests, wait until it is resolved.

Every column that refers to a person is listed with what erasure does to it in
`andai_core/erasure/registry.py`; a test fails when a new one is added without a rule.

## What stays, on purpose

- **Other people's meetings.** What the person said in someone else's meeting, their name in its
  attendees and in its minutes stay with that meeting's owner (D-61).
- **The audit trail.** Audit rows keep the person's user id (D-63); it now resolves to "Erased user".
  The erasure request rows keep ids, states and counts, never a name or address.
- **The platform's meeting tombstone.** The meeting platform keeps a content-free row for each purged
  meeting (no recording, no transcript).

## Copies that outlive the erasure

| Where | What remains | For how long |
|---|---|---|
| Postgres heap | Deleted rows stay as dead tuples in the table files until (auto)vacuum reclaims them; `VACUUM` marks the space reusable but does not scrub it from disk at once. | Until autovacuum runs on the table (minutes to hours under write load) and the pages are reused. A `VACUUM (FULL)` of the touched tables rewrites them without the dead rows if a request must be proven gone sooner. |
| Postgres WAL | The deleted rows' earlier versions are in WAL segments. | Until the segments are recycled (checkpoints), and in archived WAL until the archive's retention ends. |
| Backups | Database dumps, volume snapshots and WAL archives taken before the erasure hold the data. | Until each backup ages out under the operator's backup retention. **Restoring a backup brings erased data back:** after any restore, run every erasure finished since that backup again (`erase-user`, or the org admin's request) before the stack serves users. |
| Object storage | Recording objects are deleted by the platform's purge. | If the bucket has versioning turned on, earlier versions stay until a lifecycle rule expires them; keep versioning off for the recordings bucket or add such a rule. The Compose stack's storage (versitygw, POSIX backend, no versioning directory) keeps no versions; a deleted object's file is unlinked and its blocks are free for reuse. |
| Valkey (the platform's) | Observed on the e2e stack after an erasure: the meeting's transcript stream `tc:meeting:{platform meeting id}` (Valkey stream, **no TTL**), and its entries in the shared `transcription_segments` stream. The live segment hash `meeting:{id}:segments` (1 h TTL) was already gone. | `tc:meeting:{id}`: until someone deletes it, **open question for the platform (no delete is added here)**. `transcription_segments`: trimmed by meeting-api once consumed and older than `STREAM_RETENTION_S` (default 3600 s). Valkey's append-only file keeps both until it is rewritten. |

## Handling a data principal's request (DPDP)

1. Confirm the request comes from the person (or someone entitled to act for them) and which
   organisations hold their data.
2. If they belong to one organisation, its admin erases them from the members page. Otherwise, or if
   the organisation cannot act, the operator runs `make -C andai erase-user PROJECT=… EMAIL=…
   CONFIRM=…` and keeps the printed request ids (no address is printed or stored with them).
3. Watch `erase-status` (or the "Data erasure" section on the members page) until every request is
   `done`; a `failed` request needs investigation (`purge_failed`: the platform kept refusing a
   meeting's delete; andai-worker's log names the meeting and the platform's answer).
4. Answer the person within the period the law requires, stating what was erased, what stays and
   why (the sections above), and when the remaining copies age out of backups.
