# invocation.v1 — the meeting-bot's constructor

The bot's input, delivered as **one JSON env var `MUNSHI_BOT_CONFIG`** (ADR-0002) and validated at boot
(fail-fast). It names the meeting to join + the egress endpoints (`redisUrl` for the acts.v1 bus +
transcript egress, `meetingApiCallbackUrl` for the lifecycle.v1 sink) + transcription/recording/voice
flags + S3/auth.

## Secrets
A *config* contract legitimately carries the secrets the bot needs — `token` · `internalSecret` ·
`transcriptionServiceToken` · `s3AccessKey` · `s3SecretKey` are marked **SECRET** and appear as
**placeholders** in goldens (never real values, P14). (Contrast `transcript.v1`, a *data* contract,
which carries no auth at all.) The P15 ideal — env carries a secret-store *reference* the bot resolves
at boot — is deferred; for now the raw fields are faithful to today's wire.

## Shape
`Invocation` (`$defs`): required `platform · meetingUrl · botName · redisUrl`; everything else optional.
`automaticLeave` defaults the three timeouts. No tenancy fields (deferred, ADR-0003).

`disclosure` (optional, `$defs/Disclosure`): `{text ≤ 500 chars, privacy_notice_url (http/https)}`.
When present, the bot posts `text` followed by the link in meeting chat once it is admitted and
reports the outcome on lifecycle.v1 (`disclosure`). When absent, the bot posts nothing.

Goldens (`Invocation.<case>.json`) validated by `gate:schema`.
