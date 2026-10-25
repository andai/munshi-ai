# andai/Makefile — one-command bring-up of Munshi + the andai overlay (PLAT-01), and the image
# catalogue every service runs (PLAT-06).
#
#   make -C andai up            # the single documented bring-up command
#   make -C andai health        # every service healthy?
#   make -C andai images        # build every image in andai/images.json from this repo
#   make -C andai images-check  # the stack references only catalogue images (+ the allowlist)
#   make -C andai images-push   # push every catalogue image at VERSION to ghcr.io/sunydepalpur
#
# `make -C andai` runs with cwd=andai/, so every path below is absolute, derived from ROOT.
# Munshi's compose file keeps its own relative paths because --project-directory is deploy/compose.
# Override the env file with ENV_FILE=/abs/path (default: andai/deploy/compose/.env, git-ignored).

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ENV_FILE ?= $(ROOT)/andai/deploy/compose/.env
# Images: ghcr.io/sunydepalpur/munshi-<name>:$(VERSION) for every entry of andai/images.json, built
# for PLATFORM (the dev VM is linux/amd64; Apple silicon builds it emulated). PULL=1 makes `up` pull
# the published images instead of building missing ones. ONLY=a,b limits `images` to those entries.
VERSION := $(shell cat $(ROOT)/VERSION)
PLATFORM ?= linux/amd64
PULL ?=
ONLY ?=
IMAGES = python3 -I $(ROOT)/andai/scripts/images.py
# images-check renders the tracked template (no secrets), not the operator's .env.
CHECK_ENV_FILE ?= $(ROOT)/andai/deploy/compose/.env.example
# Shell variables override --env-file in Compose interpolation, and Munshi's compose file interpolates
# ANTHROPIC_* / CLAUDE_CODE_OAUTH_TOKEN / HOST_CLAUDE_CREDENTIALS (incl. a credentials bind mount).
# Strip them so a developer shell (e.g. one running Claude Code) can never inject an Anthropic
# endpoint or credential (D-22). The OLLM agent values come only from ANDAI_AGENT_* in ENV_FILE.
STRIP_ENV := -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL -u ANTHROPIC_MODEL -u ANTHROPIC_DEFAULT_OPUS_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL -u ANTHROPIC_DEFAULT_HAIKU_MODEL -u CLAUDE_CODE_OAUTH_TOKEN -u HOST_CLAUDE_CREDENTIALS
DC = env $(STRIP_ENV) ANDAI_ROOT=$(ROOT) docker compose -p andai-meet --project-directory $(ROOT)/deploy/compose -f $(ROOT)/deploy/compose/docker-compose.yml -f $(ROOT)/andai/deploy/compose/docker-compose.andai.yml --env-file $(ENV_FILE)
# Image variables a shell could export over the env file; images-check must see the template's values.
STRIP_IMAGE_ENV := -u IMAGE_TAG -u BROWSER_IMAGE -u AGENT_IMAGE -u AGENT_WORKER_IMAGE -u DASHBOARD_IMAGE -u COMPOSE_PROFILES
WAIT_TIMEOUT ?= 600
S ?=

.PHONY: help config config-json config-services up agent-worker images images-check images-push down ps logs health _env-ready

help:
	@echo "make -C andai up       gen secrets, build missing catalogue images (PULL=1: pull them), start, wait healthy"
	@echo "make -C andai images [ONLY=a,b] [PLATFORM=linux/amd64]   build the catalogue (andai/images.json)"
	@echo "make -C andai images-check | images-push                 stack uses only catalogue images | publish"
	@echo "make -C andai health   HTTP health on 127.0.0.1 + container states"
	@echo "make -C andai ps | logs [S=service] | down | config"
	@echo "ENV_FILE=$(ENV_FILE)"

## config — validate the merged compose config (quiet: the merged config contains secrets).
config:
	@test -f "$(ENV_FILE)" || { echo "missing $(ENV_FILE)" >&2; exit 1; }
	$(DC) config --quiet

## config-json / config-services — the merged config exactly as `up` renders it (shell-stripped).
config-json:
	@$(DC) config --format json

config-services:
	@$(DC) config --services

## _env-ready — the env file exists, is not the tracked template, and has no OLLM placeholders.
_env-ready:
	@test -f "$(ENV_FILE)" || { echo "missing $(ENV_FILE): cp andai/deploy/compose/.env.example andai/deploy/compose/.env and fill the __OLLM_*__ values" >&2; exit 1; }
	@case "$(ENV_FILE)" in *.example) echo "ENV_FILE must not be the tracked template" >&2; exit 1;; esac
	@if grep -v '^[[:space:]]*#' "$(ENV_FILE)" | grep -q '__OLLM_'; then echo "$(ENV_FILE) still has __OLLM_*__ placeholders; fill them first (andai/docs/dev-vm.md)" >&2; exit 1; fi

## up — the single bring-up command. Every Munshi, bot, agent-worker and andai image is a catalogue
## image built from this repo (andai/images.json). Images the stack needs that are not on this machine
## are built (`images`), or pulled from ghcr.io/sunydepalpur with PULL=1; nothing else is pulled
## except the allowlisted third-party images (postgres, valkey, versitygw). --no-build at start time.
up: _env-ready
	$(ROOT)/andai/scripts/gen-secrets.sh "$(ENV_FILE)"
	$(DC) config --format json | $(IMAGES) ensure --version $(VERSION) --platform $(PLATFORM) $(if $(filter 1,$(PULL)),--pull)
	$(DC) up -d --no-build --wait --wait-timeout $(WAIT_TIMEOUT)

## images — build every catalogue entry in order (bases first), tagged
## ghcr.io/sunydepalpur/munshi-<name>:$(VERSION) and :dev; prints size and build time per image.
images:
	$(IMAGES) build --version $(VERSION) --platform $(PLATFORM) $(if $(ONLY),--only $(ONLY))

## agent-worker — just the hardened andai agent-worker image (D-22), FROM munshi-agent-worker:$(VERSION).
agent-worker:
	$(IMAGES) build --version $(VERSION) --platform $(PLATFORM) --only agent-worker,agent-worker-hardened

## images-check — render the merged compose config from the tracked template (all profiles) and the
## self-hosted STT compose files, and fail on any image that is neither a catalogue image at
## $(VERSION) nor on images.json's third_party allowlist; also checks every catalogue Dockerfile's
## FROM lines. ALLOW_TEST_IMAGES=1 accepts the mock-bot:dev test alias (CI/test override only).
images-check:
	@set -e; t="$$(mktemp -d)"; trap 'rm -rf "$$t"' EXIT; \
	 env $(STRIP_IMAGE_ENV) $(MAKE) --no-print-directory -s config-json ENV_FILE=$(CHECK_ENV_FILE) COMPOSE_PROFILES='*' > "$$t/stack.json"; \
	 for f in docker-compose.yml docker-compose.cpu.yml; do \
	   env $(STRIP_IMAGE_ENV) IMAGE_TAG=$(VERSION) docker compose -f $(ROOT)/deploy/transcription/$$f config --format json > "$$t/transcription-$$f.json"; \
	 done; \
	 $(IMAGES) check --version $(VERSION) $(if $(filter 1,$(ALLOW_TEST_IMAGES)),--allow-test-images) "$$t"/*.json

## images-push — push every catalogue image at $(VERSION) (agent-worker-hardened and the andai images
## included; not the :dev or test tags) to ghcr.io/sunydepalpur. Refuses without a ghcr.io login in
## the docker config, or if any image is missing locally or is not $(PLATFORM). Log in first (the
## token goes straight from gh to docker, never onto the screen):
##   gh auth refresh -h github.com -s write:packages
##   gh auth token | docker login ghcr.io -u sunydepalpur --password-stdin
images-push:
	$(IMAGES) push --version $(VERSION) --platform $(PLATFORM)

down:
	$(DC) down

ps:
	$(DC) ps -a

logs:
	$(DC) logs --tail=200 $(S)

health:
	@$(DC) ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' | $(ROOT)/andai/scripts/health.sh "$(ENV_FILE)" -
