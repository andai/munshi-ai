# Bot-only hotfix

When a platform changes its join page or adds a bot gate, the fix is almost always in the meeting
bot (`core/meetings/services/bot`, `core/meetings/modules/join`). The bot is not a running service.
The runtime spawns one container per meeting from the image named by `BROWSER_IMAGE`. A fix can
therefore ship as **one new bot image**, with no other image rebuilt and no other service
restarted except the runtime, which only reads the new `BROWSER_IMAGE`. Bots already in meetings
keep running on the image they started with.

The canary alert usually triggers this (`canaries.md`), and the next canary confirms the fix.

## 1. Build the hotfix image

On a checkout of the release that runs (`git checkout v<VERSION>` or the deployed commit), apply
the fix on a `fix/…` branch from that release. Then build only the bot, on the release's own
join-env base, under a hotfix tag:

```sh
VERSION=$(cat VERSION)          # e.g. 0.1.0
N=1                             # hotfix number for this release
docker buildx build --platform linux/amd64 --load \
  -f core/meetings/services/bot/Dockerfile \
  --build-arg JOIN_ENV_IMAGE=ghcr.io/andai/munshi-meet-join-env:$VERSION \
  --build-arg MUNSHI_IMAGE_VERSION=$(git rev-parse --short HEAD) \
  -t ghcr.io/andai/munshi-bot:$VERSION-hotfix.$N .
```

The tag names the release it patches, so it never overwrites `munshi-bot:<VERSION>`, and rollback
is a matter of pointing back. Do not use `make -C andai images ONLY=bot VERSION=…-hotfix.N`: it
builds on `munshi-meet-join-env:<that version>`, which does not exist for a hotfix tag.

Check the image before it reaches a meeting:

```sh
pnpm --filter @munshi/bot test              # the bot's own tests
docker image inspect ghcr.io/andai/munshi-bot:$VERSION-hotfix.$N --format '{{.Architecture}}'   # amd64
```

On a deployment that pulls images, push the hotfix tag (`docker push`, after the ghcr.io login
described under `images-push` in `andai/Makefile`). Images are always built from this repository.

## 2. Point the runtime at it

In the deployment's env file:

```sh
BROWSER_IMAGE=ghcr.io/andai/munshi-bot:0.1.0-hotfix.1
```

Then run `make -C andai up` (with the deployment's `ENV_FILE`/`PROJECT`). Only the `runtime`
service's environment changed, so Compose recreates only `runtime`. `make up` accepts the hotfix
tag because the image is on the machine: it builds or pulls nothing else.

The **next spawned bot** uses the hotfix image. Bots already in a meeting keep running on the
old image. A recreated runtime adopts its live bot containers again
(`core/runtime/src/runtime_kernel/docker_backend.py`, re-adoption on start), so in-progress meetings
are not dropped.

Check it:

```sh
docker compose -p <project> exec runtime printenv BROWSER_IMAGE
```

Then send a test bot, either with the next canary or with `make -C andai live-dispatch` into a
test meeting, and confirm the container it spawned runs the hotfix image:

```sh
docker ps --format '{{.Image}} {{.Names}}' | grep munshi-bot
```

## 3. Confirm with the canary

Watch `munshi_canary_last_run_timestamp_seconds{platform="…",outcome="success"}` at andai-worker
`/metrics`. Once a canary on the broken platform succeeds, the open incident closes and the
recovery mail goes out. To skip waiting up to the canary interval, recreate andai-worker. A run
is due one interval after the previous run started, so this only helps if the interval has passed.
The faster check is a `live-dispatch` into the platform's test meeting.

## 4. Rollback

Point `BROWSER_IMAGE` back at the release image and recreate the runtime:

```sh
BROWSER_IMAGE=ghcr.io/andai/munshi-bot:0.1.0
make -C andai up
```

Bots spawned after that use the release image again. Keep the hotfix image until the next release
carries the fix, then remove it (`docker image rm ghcr.io/andai/munshi-bot:0.1.0-hotfix.1`).

## 5. Afterwards

Merge the fix into `development` through a pull request, so the next release includes it and drops
the hotfix tag. `deploy/bin/redeploy-bot.sh` is a development tool that rebuilds the bot in place
under a fixed tag. Do not use it on a deployment: it overwrites the image in place, which leaves
nothing to roll back to.
