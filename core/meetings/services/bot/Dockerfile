# syntax=docker/dockerfile:1.7
# ==============================================================================
# @munshi/bot — the v0.12 carved meetings WORKER (P7 ephemeral container).
#
# Boots from MUNSHI_BOT_CONFIG (invocation.v1), joins a meeting via @munshi/join over
# a @munshi/remote-browser page, captures → @munshi/{gmeet,mixed}-pipeline →
# @munshi/transcribe-whisper, records via @munshi/recording, then dies.
#
# Two stages, BOTH on `munshi/meet-join-env:dev` (the published Playwright +
# humanized-X11 env, node 22 + corepack, chromium prebaked in /ms-playwright):
#   • builder  — corepack pnpm, install the whole v0.12 workspace, build @munshi/bot
#                and its workspace deps (pnpm --filter @munshi/bot... build).
#   • runtime  — same base, carry the built workspace tree + node_modules, add the
#                bot's own audio deps (pulseaudio/fluxbox), run node dist/index.js
#                under Xvfb/PulseAudio via the entrypoint.
#
# Building ON the env image keeps node ABI + the pnpm symlink farm valid across
# stages (same base layout), and reuses the browsers already in /ms-playwright
# (PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1) instead of re-fetching them.
#
# Build context = the v0.12 repo root (pnpm-workspace.yaml is there). Build with:
#   docker build -t ghcr.io/sunydepalpur/munshi-bot:v012 -f meetings/services/bot/Dockerfile .
# ==============================================================================

# JOIN_ENV_IMAGE — the join-env base both stages build on. The default is the local dev tag that
# `make -C deploy/compose bot` builds; `make -C andai images` passes the versioned catalogue image.
ARG JOIN_ENV_IMAGE=munshi/meet-join-env:dev

# ------------------------------------------------------------------------------
# Stage 1: builder — install + build the workspace slice @munshi/bot depends on.
# ------------------------------------------------------------------------------
FROM ${JOIN_ENV_IMAGE} AS builder

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    # Browsers are already in the env image (/ms-playwright) — never re-download
    # them during `pnpm install` (playwright/remote-browser have a postinstall).
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # onnxruntime-node (via @munshi/mixed-pipeline) — skip the optional CUDA fetch
    # (on linux/x64 its postinstall otherwise downloads the CUDA EP from NuGet);
    # the segmenter runs CPU and the smoke/gmeet path never loads it anyway.
    ONNXRUNTIME_NODE_INSTALL=skip \
    CI=1

# pnpm pinned to the workspace's packageManager (pnpm@11.7.0) via corepack.
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate && pnpm --version

WORKDIR /build

# Workspace manifests first (best layer cache for the install step). Copying the
# whole tree is simplest+robust for a carved monorepo; the dep layer still caches
# on the lockfile via the cache mount below.
COPY pnpm-workspace.yaml pnpm-lock.yaml package.json tsconfig.base.json turbo.json ./
# The source the bot build needs: the five backend domains under core/ (the bot + its @munshi/* workspace
# deps all live under core/meetings/modules/*). The other pnpm-workspace globs
# (integrations/* · clients/* · sdks/* · tools/*) are green-on-empty: a fresh git clone omits empty
# dirs and the bot depends on none of them, so they are intentionally NOT copied (COPYing an absent
# dir fails the build).
COPY core ./core
# NB: `scripts/` and `schemas/` are pnpm-workspace globs that are green-on-empty in this carved tree
# (no packages) — they aren't present in the open-core repo, so they're intentionally not COPYed here.

# Install the whole workspace. --frozen-lockfile=false: tolerate minor lockfile
# drift in this carved build (we're proving the image, not gating the lockfile).
# allowBuilds in pnpm-workspace.yaml lets esbuild/onnxruntime-node/sharp/protobufjs
# run their native postinstall; the playwright postinstall no-ops (SKIP flag above).
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile=false

# Build the page-side capture bricks FIRST. These are NOT bot dependencies
# (gate:isolation — they are a BROWSER bundle, not a Node import), so the
# `@munshi/bot...` selector below does NOT pull them. The bot's own `build` script
# runs build-browser-utils.mjs, which esbuild-bundles these bricks' dist into
# window.MunshiBrowserUtils (the global capture-bridge.ts injects via addInitScript) —
# so their dist/ must already exist when the bot build runs.
RUN pnpm --filter "@munshi/capture-codec" --filter "@munshi/gmeet-capture" --filter "@munshi/mixed-capture-core" --filter "@munshi/record-chunker" --filter "@munshi/jitsi-capture" --filter "@munshi/teams-capture" --filter "@munshi/zoom-capture" run build

# Build @munshi/bot and everything it depends on (the `...` selector pulls the deps;
# pnpm runs them in topological order, each package's own `build` = tsc → dist/).
# The bot's build = `tsc && node build-browser-utils.mjs`, so this also emits the
# capture bundle dist/browser-utils.global.js (the bricks above are now built).
RUN pnpm --filter "@munshi/bot..." run build

# Sanity: the entry the runtime will exec + the capture bundle must exist after the build.
RUN test -f /build/core/meetings/services/bot/dist/index.js \
    && echo "[builder] @munshi/bot dist/index.js present" \
    && test -s /build/core/meetings/services/bot/dist/browser-utils.global.js \
    && echo "[builder] capture bundle browser-utils.global.js present ($(wc -c < /build/core/meetings/services/bot/dist/browser-utils.global.js) bytes)"

# Bake the mixed-lane diarization model (onnx-community/pyannote-segmentation-3.0)
# into an image-resident HF cache so the Zoom/Teams lane loads it OFFLINE at
# runtime — no HuggingFace download stall mid-meeting (prep for O6). The builder
# has network, so this downloads + caches into /opt/hf-cache; the runtime stage
# copies that dir and points MUNSHI_HF_CACHE at it (chunked-transcriber.ts reads it).
ENV MUNSHI_HF_CACHE=/opt/hf-cache
# The script self-resolves @huggingface/transformers via the mixed-pipeline
# package (the bot doesn't list it as a direct dep — it's transitive), so CWD
# doesn't matter; run from /build for a stable path to the script.
RUN cd /build && node core/meetings/services/bot/warm-hf-cache.mjs

# ------------------------------------------------------------------------------
# Stage 2: runtime — the ephemeral worker. Same env base; add the bot's own audio
# deps; carry the built workspace tree (incl. node_modules symlink farm) intact.
# ------------------------------------------------------------------------------
FROM ${JOIN_ENV_IMAGE} AS runtime

# The bot's OWN runtime deps NOT in the meet-join env: PulseAudio (the speak/TTS
# + capture audio chain), fluxbox (a WM for the X11 session), and the aws CLI —
# the authenticated-bot session store (@munshi/remote-browser session-store.ts)
# shells out to `aws s3` to restore the signed-in profile before launch and write
# it back on clean teardown; without the binary every authenticated spawn fails
# at session-restore. The Playwright + humanized-X11 stack (xvfb·xdotool·xclip·
# x11vnc·websockify) comes from the base.
# noble (#1011) dropped `awscli` from the archive — `apt-get install awscli` has no
# installation candidate there, which broke this image the moment the base moved. AWS's
# own v2 installer is the supported path on 24.04; it is the same binary the session
# store shells out to, so nothing above this line changes.
RUN export DEBIAN_FRONTEND=noninteractive \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       pulseaudio \
       fluxbox \
       socat \
       ca-certificates \
       curl \
       unzip \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip \
    && rm -rf /var/lib/apt/lists/* \
    && aws --version

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    NODE_ENV=production

# Which build this is. It is stamped into every captured-signal tape header and into each sidecar's
# mini-header, because a prod fixture outlives the deployment that produced it: the same observation
# ("the bot dropped the tail") means opposite things on either side of a fix, and a tape that cannot
# name its build proves a behaviour it cannot attribute. The build passes the git sha
# (`--build-arg MUNSHI_IMAGE_VERSION=$(git rev-parse --short HEAD)`); an unstamped image records
# 'unknown', which is the honest answer rather than a guess.
ARG MUNSHI_IMAGE_VERSION=unknown
ENV MUNSHI_IMAGE_VERSION=${MUNSHI_IMAGE_VERSION}

WORKDIR /app

# Carry the entire built workspace (sources, dist/, and the pnpm node_modules
# symlink farm). Same base ⇒ the workspace symlinks (node_modules/@munshi/* →
# ../../meetings/modules/*) and the .pnpm hardlink store stay valid; no install
# in runtime. The bot is run from its package dir so its relative schema-path
# (src→../../../contracts/invocation.v1) and pnpm-linked deps resolve.
COPY --from=builder /build /app

# The page-side capture bundle, at the EXACT path capture-bridge.ts injects via
# addInitScript: BROWSER_UTILS_PATH = '/app/browser-utils.global.js'. It is already
# in the tree above (meetings/services/bot/dist/), but the bot resolves it from
# /app, so place it there explicitly so window.MunshiBrowserUtils is present on every
# page navigation (the gmeet per-channel + mixed-lane capture wiring depends on it).
COPY --from=builder /build/core/meetings/services/bot/dist/browser-utils.global.js /app/browser-utils.global.js

# The baked HF model cache (mixed-lane pyannote segmentation), carried from the
# builder. MUNSHI_HF_CACHE points the @huggingface/transformers env.cacheDir here
# so the Zoom/Teams diarizer loads the model from disk with NO network at runtime.
COPY --from=builder /opt/hf-cache /opt/hf-cache
ENV MUNSHI_HF_CACHE=/opt/hf-cache
# License hygiene: the baked weights are onnx-community/pyannote-segmentation-3.0
# (MIT). MIT requires the copyright + permission notice travel with the software,
# so ship it next to the cache. Recorded in repo THIRD_PARTY_LICENSES.md + the
# per-release SPDX SBOM (scripts/sbom.mjs); model weights sit outside gate:licenses.
COPY licenses/onnx-community-pyannote-segmentation-3.0.LICENSE.txt /opt/hf-cache/LICENSE.pyannote-segmentation-3.0

# Entrypoint: bring up Xvfb + PulseAudio (the env the browser/capture/speak path
# expects), then run the worker. Boot → validate config → launch browser → join.
COPY core/meetings/services/bot/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# noVNC (browser view) — present in the env base; harmless if unused.
EXPOSE 6080

ENTRYPOINT ["/app/entrypoint.sh"]
