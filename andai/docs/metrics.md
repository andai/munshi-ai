# Metrics

munshi.ai's own services expose Prometheus metrics at `GET /metrics` (OPS-05). This page lists
every series, its labels with their closed value sets, what it means and what reads it. The
dashboards and alert rules that use them ship separately; GPU utilisation comes from the GPU
exporter there, not from these services.

## Where to scrape

| Service | Inside the network | On the host (127.0.0.1 only) | What it serves |
|---|---|---|---|
| andai-worker | `andai-worker:8000/metrics` | `${ANDAI_WORKER_PORT:-18301}` | Everything read from the database (joins, canaries, pipelines, intake, deletion, minutes, sealed values, database size), then its own mail and ASR-lag counters. 503 without a database URL. |
| andai-core | `andai-core:8000/metrics` | `${ANDAI_CORE_PORT:-18300}` | Its own mail counters (invites, password resets). |
| andai-intake | `andai-intake:8000/metrics` | `${ANDAI_INTAKE_PORT:-18302}` | The SMTP receiver's verdicts and its confirmation mail counters. |

`/metrics` sits outside `/api`. The portal forwards `/api/*` only and refuses `.` and `..` path
segments, so a browser cannot reach `/metrics` through the portal. It needs no session, and the
two-factor stage gate passes it by its exact path. Compose publishes the services on 127.0.0.1
only. Do not put `/metrics` behind a public ingress.

Database-derived series come from andai-worker alone, so they are scraped once. The worker runs
one process per replica, and so does every service today: each process counts its own counters and
Prometheus adds the replicas up. More than one uvicorn worker per container would need
`PROMETHEUS_MULTIPROC_DIR`. Nothing sets that up yet.

## The rule: labels never carry identity or content

Every label name is one of `platform`, `source`, `outcome`, `reason`, `stage`, `kind`, `result`,
`status`, `table`, `key_id` and `le` (`andai_core.metrics.LABEL_ALLOWLIST`). Every value comes from
a closed set in the code or a database CHECK constraint, and matches `^[a-z0-9_.:+-]{0,40}$`. No
org, team, user, meeting, email, link, IP address, title, subject or free text ever becomes a
label. `andai/services/worker/tests/test_metrics_hygiene.py` scrapes all three services after
seeding 50 orgs, 200 users and 500 meetings full of identifying data. It fails on a label outside
the list, a value of the wrong shape or one that looks like an email, UUID, URL or IP, a family
with more than 200 series, or any seeded string in the text. Later phases (provider keys, agents)
follow the same rule: add the label name to the allowlist only with a closed value set, and extend
this page.

## Closed value sets

| Label | Values |
|---|---|
| `platform` | `google_meet`, `teams`, `zoom` |
| `source` | `portal_link`, `invite_mail`, `teams_app`, `meet_addon`, `ics_sync`, `canary`; `all` in the ratio |
| `outcome` (joins) | `success`, `failure`, `excluded`, `pending` |
| `outcome` (canaries) | `success`, `failure` |
| `outcome` (mail) | `sent`, `failed` |
| `outcome` (intake) | `scheduled`, `updated`, `cancelled`, `joined_now`, `quarantined`, `ignored`, `` (still open) |
| `reason` (joins) | `JoinFailureReason`: `admission_denied`, `admission_timeout`, `meeting_not_found`, `meeting_ended`, `auth_required`, `blocked_by_policy`, `removed_by_participant`, `ui_changed`, `capacity_exhausted`, `internal_error`; `cancelled`; `` (success, pending) |
| `reason` (intake) | `sender_unverified`, `unknown_user`, `no_meeting_link`, `parse_error`, `rate_limited`, `` (not quarantined) |
| `stage` | `dispatch`, `ingest`, `minutes`, `recap`, `deletion`, `intake` (durations: the first four) |
| `status` | `received`, `processing`, `done`, `quarantined`, `ignored` |
| `kind` | `invite`, `password_reset`, `recap`, `alert`, `confirmation`, `other` |
| `result` | `accept`, `unknown_rcpt`, `too_many_rcpt`, `too_large`, `store_failed` |
| `table` | `user_totp`, `platform_principals` (`andai_core.sealed.SEALED_COLUMNS`) |
| `key_id` | `kek-` and 16 hex digits: a truncated hash of a KEK, never the key |

## Catalogue

### Bot joins and canaries (andai-worker)

| Series | Type | Labels | Meaning | Used by |
|---|---|---|---|---|
| `munshi_bot_joins_total` | gauge | platform, source, outcome, reason | Bot joins started in the last 24 h, classified per D-39. | Join dashboard |
| `munshi_join_success_ratio_24h` | gauge | platform, source | successes / (successes + failures) over 24 h; absent while nothing counted. `source="all"` is what the alert reads. | Join-rate alert (< 95%), worker alert mail |
| `munshi_canary_consecutive_failures` | gauge | platform | Canary failures in a row up to the latest run. | Canary alert (≥ 2), worker alert mail |
| `munshi_canary_last_run_timestamp_seconds` | gauge | platform, outcome | Start of the latest canary run with that outcome. | Canaries-stopped alert |

### Pipelines (andai-worker)

| Series | Type | Labels | Meaning | Used by |
|---|---|---|---|---|
| `munshi_pipeline_pending` | gauge | stage | Work still owed per stage (below). | Backlog dashboard |
| `munshi_pipeline_oldest_pending_seconds` | gauge | stage | Seconds since the oldest pending item became due; 0 when none. | Backlog-age alerts (e.g. dispatch past its lead, deletion > 900 s) |
| `munshi_pipeline_failed_24h` | gauge | stage | Items that failed for good in the last 24 h. | Failure alerts |
| `munshi_pipeline_stage_seconds` | histogram | stage | How long a stage took, for items it finished in the last 24 h (at most 5000 per stage). Buckets 1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600 s. | Stage-latency dashboard (p50/p95) |

What each stage counts (`andai_pipeline_stats`, `andai_pipeline_durations`, migration 0018):

| Stage | Pending, due since | Failed in 24 h, dated by | Duration |
|---|---|---|---|
| `dispatch` | Scheduled meetings, not skipped or deleted, still `scheduled` and due: since `scheduled_start − ANDAI_INTAKE_JOIN_LEAD_S` | Ended `failed` with no bot sent, by `ended_at` | Due → bot recording (`started_at`) |
| `ingest` | Finished meetings with a platform id whose transcript copy is owed: since the end | `transcript_state = failed`, by `ended_at` | End → transcript stored |
| `minutes` | Finished, transcript stored, minutes owed (not deleted, not canary): since the transcript was stored | `mom_state = failed`, by `ended_at` | Transcript stored → first minutes |
| `recap` | Recap mail owed (not deleted, not canary): since the first minutes | `recap_state = failed`, by `ended_at` | First minutes → recap sent |
| `deletion` | Deleted meetings waiting for their purge: since deletion | Purge gave up, by `deleted_at` | — |
| `intake` | Inbound messages `received` or `processing` (org-less ones too): since received | Quarantined `parse_error` after 5 claims (processing gave up), by `received_at` | — |

Minutes stay owed while the language model route is not configured (`ANDAI_LLM_BASE_URL` empty), so
`minutes` grows on such a deployment; that is expected there.

### Minutes, deletion and sealed values (andai-worker)

| Series | Type | Labels | Meaning | Used by |
|---|---|---|---|---|
| `munshi_mom_pending` | gauge | — | Finished meetings with a stored transcript whose minutes are owed. | Minutes dashboard |
| `munshi_mom_failed_total` | gauge | — | Meetings whose minutes failed after every attempt. | Minutes dashboard |
| `munshi_mom_latency_seconds` | histogram | — | Meeting end → first minutes, for minutes stored in the last 24 h. | SC2 p95 ≤ 10 min |
| `munshi_deletions_pending` | gauge | — | Deleted meetings not yet purged (pending or failed). | Deletion alert |
| `munshi_deletion_oldest_pending_seconds` | gauge | — | Seconds since the oldest unpurged deletion; above 900 misses PORT-04. | Deletion alert (> 900 s) |
| `munshi_sealed_values` | gauge | table, key_id | Sealed values per table still wrapped by each KEK; read at most every 300 s. | Key rotation (`runbooks/key-rotation.md`) |

### User erasure (andai-worker)

| Series | Type | Labels | Meaning | Used by |
|---|---|---|---|---|
| `munshi_erasures_pending` | gauge | — | User erasure requests not yet done (pending or running), from `andai_erasure_backlog` (0019). | Erasure dashboard |
| `munshi_erasure_oldest_pending_seconds` | gauge | — | Seconds since the oldest open erasure request was made (0 when none); above 86400 the 24-hour SLA is missed (`erasure.md`). | Erasure-SLA alert (> 86400 s) |
| `munshi_erasures_failed` | gauge | — | Erasure requests that failed (`purge_failed`, `internal_error`) and need the operator. | Erasure-failure alert (> 0) |

### Intake (andai-worker and andai-intake)

| Series | Type | Labels | Meaning | Used by |
|---|---|---|---|---|
| `munshi_intake_messages_24h` | gauge | status, outcome, reason | Messages received in the last 24 h (org-less ones too) by status, outcome and quarantine reason. Served by andai-worker. | Intake dashboard, quarantine-spike alert |
| `munshi_intake_smtp_total` | counter | result | The SMTP receiver's verdicts: one per refused recipient, one per message accepted, too large (552, declared SIZE or DATA past the limit) or not stored (451). Served by andai-intake. | Intake dashboard, store-failure alert |

### Mail and speech-to-text (every service, per process)

| Series | Type | Labels | Meaning | Used by |
|---|---|---|---|---|
| `munshi_mail_sent_total` | counter | kind, outcome | Mail handed to the SMTP relay: `sent` (accepted) or `failed` (refused or unreachable). andai-core counts invites and password resets, andai-worker recaps and alerts, andai-intake confirmations. Every kind/outcome pair is present from start-up at 0. | Mail-failure alert |
| `munshi_asr_segment_lag_seconds` | histogram | — | For each transcript segment andai copies (andai-worker), the platform's write time of the segment minus its spoken end. Buckets 0.5, 1, 2, 3, 5, 10, 15, 30, 60 s. | ASR-latency dashboard |
| `munshi_asr_segment_lag_missing_total` | counter | — | Segments copied without both times, so no lag was observed. | ASR-latency dashboard |

The ASR lag is the platform's segment write lag, not speech-to-screen latency. **Today it is
empty in practice:** the platform's transcript answer for a finished meeting is read from its
Postgres store, and those segments carry no `created_at` (the platform's db-writer drops it), so
every real segment counts in `munshi_asr_segment_lag_missing_total`. Read a rising missing count
with an empty histogram as "no data", never as zero lag.

### Storage (andai-worker)

| Series | Type | Labels | Meaning | Used by |
|---|---|---|---|---|
| `munshi_db_size_bytes` | gauge | — | The andai database's size on disk (`pg_database_size`). | Storage dashboard; disk-full alerts read the node exporter's filesystem metrics |

## Reading it by hand

```sh
curl -s http://127.0.0.1:${ANDAI_WORKER_PORT:-18301}/metrics | grep '^munshi_pipeline'
curl -s http://127.0.0.1:${ANDAI_CORE_PORT:-18300}/metrics | grep '^munshi_mail'
curl -s http://127.0.0.1:${ANDAI_INTAKE_PORT:-18302}/metrics | grep '^munshi_intake'
```
