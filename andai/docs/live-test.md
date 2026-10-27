# Live tests on a Mac

How to send the real meeting bot into a live **test** meeting from a Mac running the whole stack
under Docker Desktop, and read how it went: every status change of the bot and the lag of every
transcript segment. The tool is `andai/scripts/live_dispatch.py`, run through
`make -C andai live-dispatch`.

## Rules (D-21)

Until the backends behind the OLLM speech-to-text and chat routes are verified AND AI-hosted:

- **Test meetings only.** Meetings you host for the test, never a real customer or internal meeting.
- **Synthetic or consenting speech only.** Read a script yourself, play generated audio, or have
  colleagues who agreed to it speak. Nobody joins who has not agreed.
- The bot joins as **munshi.ai (recording)**: it always says it records. Teams only accepts letters,
  numbers, spaces and `- ' . _ @` in a guest name, so on Teams it joins as **munshi.ai - recording**.
  Admit it only into a test meeting.

## 1. Docker Desktop

| Setting | Value |
|---|---|
| Resources → CPUs | 6 or more (each bot runs Chromium) |
| Resources → Memory | 12 GB or more |
| Resources → Disk | 100 GB or more (the bot image alone is ~3.6 GB) |
| General | **Use Rosetta for x86_64/amd64 emulation on Apple Silicon** on (Apple Silicon Macs) |

The catalogue images are `linux/amd64` (`PLATFORM` in `andai/Makefile`); on Apple silicon they
run under Rosetta. Building them on the Mac (`make -C andai up` builds what is missing) takes a
while; `make -C andai up PULL=1` pulls the published images from ghcr.io instead.

One stack at a time: every env file uses the compose project `andai-meet` and the same loopback
ports, so stop another stack (for example a mock-bot stack) with `make -C andai down
ENV_FILE=<its env file>` first.

## 2. The env file

Live runs use their own env file outside the repository's tracked files, `.work/.local/env.live`
(`.work/` is git-ignored):

```bash
mkdir -p .work/.local
cp .env.example .work/.local/env.live
chmod 600 .work/.local/env.live
```

Open it in an editor and replace the placeholders yourself (the values never go into chat, a
commit, a log or a screenshot):

| Placeholder | Meaning |
|---|---|
| `__OLLM_BASE_URL__` | OLLM origin (+ path prefix if any), no trailing slash, no `/v1` |
| `__OLLM_API_KEY__` | OLLM API key |
| `__OLLM_STT_MODEL__` | Whisper model id served on `/v1/audio/transcriptions` |
| `__OLLM_CHAT_MODEL__` | chat model id served on `/v1/chat/completions` |
| `__OLLM_AGENT_MODEL__` | model id the agent harness requests on `/v1/messages` |

Leave everything else as the template has it. In particular keep
`BROWSER_IMAGE=ghcr.io/andai/munshi-bot:<VERSION>` (the real bot, not the `mock-bot:dev` test
image) and leave the stack secrets (`ADMIN_TOKEN`, `INTERNAL_API_SECRET`, …) empty: `make -C andai up`
generates them into this file. Check that no placeholder is left:

```bash
grep -c __OLLM_ .work/.local/env.live      # 0
```

## 3. Bring the stack up

From the repository root:

```bash
make -C andai up ENV_FILE=$PWD/.work/.local/env.live
make -C andai health ENV_FILE=$PWD/.work/.local/env.live
```

`health` must print `OK` for every service and no failing container. Pass `ENV_FILE` as an
absolute path to every target.

## 4. Send the bot

Host a test meeting (Google Meet, Microsoft Teams or Zoom), copy its link, then:

```bash
make -C andai live-dispatch ENV_FILE=$PWD/.work/.local/env.live LANGUAGE_CODE=en \
  URL='https://meet.google.com/abc-defg-hij' \
  LOG=$PWD/.work/live/meet-en-$(date -u +%Y%m%dT%H%M%SZ).log
```

| Variable | Meaning |
|---|---|
| `URL` | the meeting link, in single quotes (a link with `&` or `?` breaks unquoted) |
| `LANGUAGE_CODE` | the language spoken, e.g. `en` or `hi` (default `en`; not `LANG`, which is the shell locale) |
| `STOP_AFTER` | stop the bot after this many seconds (default: until Ctrl-C or the meeting ends) |
| `LOG` | an absolute path; the output lines are appended there too |

What happens:

1. The harness makes sure the operator test principal `live-test@principals.munshi.invalid`
   exists on the platform with diagnostic signal tapes off, and mints an API key (`bot,tx`) for
   this run. The key is revoked when the run ends.
2. It sends the bot (`POST /bots` on the local gateway) with the link and the language.
3. Admit **munshi.ai (recording)** (on Teams, **munshi.ai - recording**) from the meeting's lobby,
   then speak.
4. It follows the meeting over the gateway's `/ws` feed and by polling the meeting every 5 s.
5. Ctrl-C (or `STOP_AFTER`) stops the bot and waits up to 90 s for the meeting to end; a second
   Ctrl-C leaves without waiting. If the meeting ends first (the bot is removed, the host ends the
   call, a join fails), the harness ends with it.

## 5. Reading the output

One JSON object per line, each with a UTC `ts` and an `event`:

| `event` | Fields |
|---|---|
| `start` | `meeting` (link cut to its host and the code's first three characters), `language`, `stt_route` (`ollm:<hash8>`, never the URL), `stt_model` |
| `principal_ready` | the run's key scopes, `capture_signal: false` |
| `dispatched` | `meeting_id`, `platform`, `meeting_code` (first three characters) |
| `status` | `status` (`requested`, `joining`, `awaiting_admission`, `active`, `needs_help`, `stopping`, `completed`, `failed`), `source` (`dispatch`, `ws`, `poll`), and on a terminal status `completion_reason` / `failure_stage` |
| `draft` | the first sight of a segment still being spoken, with its lag so far |
| `segment` | a finished segment: `lag_s`, `language`, `speaker`, `text` (first 120 characters) |
| `stop_sent` | why the harness stopped the bot, and the gateway's HTTP status |
| `summary` | `final_status`, `reached_active`, segment counts, `first_segment_after_active_s`, and `lag_s` (`count`, `first`, `median`, `p95`, `max`) |
| `ws_subscribed`, `ws_dropped`, `ws_error`, `poll_error`, `key_revoked`, `setup_error` | plumbing |

**Lag** is the time the segment reached the harness minus the end of its speech
(`absolute_end_time`): how far the live transcript trails the speaker. It needs the Mac's clock and
the bot container's clock to agree, which they do on one Docker Desktop host.

The harness exits 0 when the bot reached `active`, 1 when it never did, 2 when it could not start
(env file, stack or dispatch refused; the reason is printed). Through `make` any non-zero exit shows
as make's own error status 2.

## 6. Logs and secrets

- The harness reads only `ADMIN_TOKEN`, the admin-api and gateway ports, `TRANSCRIPTION_SERVICE_URL`
  and `TRANSCRIPTION_MODEL` from the env file. It never reads the OLLM key.
- Every line passes through a redactor: the admin token, the run's key, the OLLM URL and host, the
  full meeting link and the full meeting code never appear in the output or the log.
- Keep logs in the git-ignored `.work/` tree. They hold the transcript text of the test speech;
  never commit them.
- Check a log before sharing it: `grep -cF "$(sed -n 's/^ADMIN_TOKEN=//p' .work/.local/env.live)" <log>`
  prints `0`.

## 7. Finish

```bash
make -C andai down ENV_FILE=$PWD/.work/.local/env.live
```

Volumes are kept. The test principal stays on the platform (no key left on it) and is reused by
the next run.
