<div align="center">

<img src="assets/logodark.svg" alt="munshi.ai logo" width="72"/>

# munshi.ai

**A self-hosted, open-source AI meeting assistant for Google Meet, Microsoft Teams and Zoom.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

</div>

---

munshi.ai sends a notetaker bot into your Google Meet, Microsoft Teams and Zoom calls, transcribes
the conversation in English, Hindi and Hinglish, and turns it into minutes of meeting (MoM) and
action items. Organisations, teams and two-factor authentication are built in, and everything runs
on your own infrastructure: audio, transcripts and summaries never leave it, and speech-to-text and
the language model are endpoints you host.

## Status

**Early development — Phase 1: platform baseline.** What works today is the meeting platform this
project is built on (bot dispatch, live transcription, the gateway, admin and meeting APIs, the
agent runtime, the terminal and the operator dashboard), renamed to munshi.ai and wired to an
in-house OpenAI/Anthropic-compatible endpoint. The munshi.ai services under `andai/` (core API,
post-meeting worker, invite intake, portal) are health-only skeletons. Hinglish speech recognition,
teams and 2FA, the MoM pipeline and the portal arrive in later phases. Do not use it for real
meetings yet: until each model route is verified to be hosted in-house, use test meetings and
synthetic content only.

## Architecture

```
                        Google Meet · Microsoft Teams · Zoom
                                        ▲
                                        │ bot joins, captures audio
 ┌──────────────────────────────────────┴───────────────────────────────────────────┐
 │ your infrastructure                                                              │
 │                                                                                  │
 │  portal (andai/portal) ──► core API (andai/services/core) ──► gateway            │
 │                               │                                 │                │
 │  worker (MoM, action items) ◄─┘ webhooks         admin-api · meeting-api         │
 │  intake (calendar invites)                       runtime ──► bot containers      │
 │                                                  agent-api · flows · MCP         │
 │                                                  terminal · dashboard (127.0.0.1)│
 │                                                                                  │
 │  Postgres · Valkey · S3-compatible storage (versitygw)                           │
 │                                                                                  │
 │  in-house model endpoint (OpenAI- and Anthropic-compatible):                     │
 │    /v1/audio/transcriptions (Whisper STT) · /v1/chat/completions · /v1/messages  │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

- `core/` — the meeting platform: gateway, identity (admin-api), meetings (bot, meeting-api,
  transcription, MCP), runtime (container orchestration), agent (agent-api, worker) and flows.
- `clients/` — the terminal workbench, the browser extension and the slim Python client.
- `services/dashboard` — the operator dashboard (bound to 127.0.0.1).
- `andai/` — munshi.ai's own services, portal, compose overlay and operator scripts.
- `deploy/` — Docker Compose (the default) and Helm.

## Quick start

Requirements: Docker with Compose v2, GNU make, Python 3, and an OpenAI/Anthropic-compatible
endpoint you host (Whisper speech-to-text on `/v1/audio/transcriptions`, chat on
`/v1/chat/completions`, and a tool-calling model on `/v1/messages`).

```bash
git clone https://github.com/andai/munshi-ai.git
cd munshi-ai
cp .env.example .env && chmod 600 .env
# edit .env: replace every __OLLM_*__ placeholder with your endpoint, API key and model ids
# (.env.example documents every setting and which containers read it)
make -C andai up       # generates secrets, pulls/builds images, starts everything, waits for health
make -C andai health   # every service healthy?
```

`make -C andai down` stops the stack. The full single-VM guide is
[`andai/docs/dev-vm.md`](andai/docs/dev-vm.md); self-hosted speech-to-text for production is in
[`andai/docs/stt-self-hosted.md`](andai/docs/stt-self-hosted.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues privately as described in
[SECURITY.md](SECURITY.md).

## License

munshi.ai is licensed under the [Apache License 2.0](LICENSE). It includes software originally
developed by a third party under the same license; see [NOTICE](NOTICE) for attribution and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for artifacts baked into the images.
