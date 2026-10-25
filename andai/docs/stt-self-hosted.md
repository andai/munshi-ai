# Self-hosted STT (production option)

In dev, Munshi's meeting bots and the terminal's dictation send audio to the AND AI OLLM endpoint's
Whisper route (`TRANSCRIPTION_SERVICE_URL=__OLLM_BASE_URL__`, D-19). For production, or whenever an
OLLM STT route cannot be shown to be AND AI-hosted (D-21), switch STT to Munshi's own transcription
unit in `deploy/transcription`. This is a documented option only: Phase 1 does not deploy it.

Nothing in the andai overlay changes for this switch. It is three values in
`andai/deploy/compose/.env` plus a separately deployed unit.

## What the unit is

`deploy/transcription` (Munshi v0.12.28) is its own compose project, not part of `make -C andai up`:

| File | Use |
|---|---|
| `deploy/transcription/docker-compose.yml` | GPU: nginx load balancer `transcription-api` + `transcription-worker-1` (faster-whisper, `DEVICE=cuda`, needs nvidia-container-toolkit). Image `ghcr.io/andai/munshi-transcription:${IMAGE_TAG}`. |
| `deploy/transcription/docker-compose.cpu.yml` | CPU-only variant. Image `ghcr.io/andai/munshi-transcription-cpu:${IMAGE_TAG}`. |
| `deploy/transcription/.env.example` | `TRANSCRIPTION_LB_PORT` (default 8083), `MODEL_SIZE` (default `large-v3-turbo`), `COMPUTE_TYPE`, `CPU_THREADS`, `IMAGE_TAG`, `API_TOKEN`. |
| `deploy/transcription/README.md` | Upstream run and scaling notes (more workers = uncomment worker 2/3 in the compose file **and** `nginx.conf`, one GPU each). |

The unit exposes the same OpenAI-compatible route the OLLM endpoint does:
`POST /v1/audio/transcriptions`. It ignores the request's `model` field; the model that runs is the
unit's `MODEL_SIZE`.

## Deploy it

On the STT host (a GPU host for production; the CPU file works for small loads):

```bash
cd deploy/transcription
cp .env.example .env
# Set at least:
#   IMAGE_TAG=0.1.0             (the repo's VERSION; do not run :dev)
#   API_TOKEN=$(openssl rand -hex 32)
#   MODEL_SIZE=large-v3-turbo   (multilingual; covers English and Hindi)
docker compose up -d                              # GPU
# or: docker compose -f docker-compose.cpu.yml up -d
docker compose logs -f                            # wait for "Model loaded successfully"
curl -fsS http://127.0.0.1:8083/health
```

Perimeter rules that apply here (PLAT-02, D-15):

- **Model weights come from an internal store, not the internet.** On first start the worker
  downloads faster-whisper weights into the `transcription-models` volume (`/app/models`). For
  production, pre-seed that volume from AND AI's internal model mirror (and record the model and
  its license), and run the unit without outbound internet so a missing model fails instead of
  downloading.
- **Bind the load balancer privately.** The upstream compose file publishes `8083` on all
  interfaces. Restrict it to the private network (security group / firewall to the meeting stack
  host only), or front it with the same private network as the stack.
- **Always set `API_TOKEN`.** Empty means the endpoint is open.

## Point the stack at it

In `andai/deploy/compose/.env` on the stack host:

```dotenv
# base URL only; the bot client appends /v1/audio/transcriptions
TRANSCRIPTION_SERVICE_URL=http://<stt-host-private-ip>:8083
# must equal API_TOKEN in deploy/transcription/.env
TRANSCRIPTION_SERVICE_TOKEN=<that API_TOKEN>
# leave empty: the unit ignores it (MODEL_SIZE decides)
TRANSCRIPTION_MODEL=
```

If the unit runs on the same Docker host as the stack, use the host's private IP (or attach both
projects to one shared external Docker network and use `http://transcription-api`).

Then apply and check:

```bash
make -C andai up        # recreates meeting-api and terminal with the new STT values
make -C andai health
```

The agent LLM (`MUNSHI_LLM_*`) is independent of this switch and keeps pointing at the OLLM endpoint.

## Verify

- `curl -fsS -H "Authorization: Bearer <API_TOKEN>" -F file=@andai/tests/fixtures/audio/en-smoke.wav
  -F model=whisper-1 http://<stt-host-private-ip>:8083/v1/audio/transcriptions` returns text
  (fixtures arrive with the OLLM probe work).
- A test meeting produces transcript segments, and the STT host's logs show the requests.
- Record the switch (date, host, `MODEL_SIZE`, image digest) in the deployment evidence.
