# andai/Makefile — one-command bring-up of Munshi + the andai overlay (PLAT-01), and the image
# catalogue every service runs (PLAT-06).
#
#   make -C andai up            # the single documented bring-up command
#   make -C andai health        # every service healthy?
#   make -C andai db-init       # create andai-core's database and roles (idempotent; `up` runs it)
#   make -C andai db-migrate    # migrate andai-core's database to head (`up` runs it)
#   make -C andai bootstrap EMAIL=owner@example.com ORG="Example Ltd"   # first org + owner, once
#   make -C andai images        # build every image in andai/images.json from this repo
#   make -C andai images-check  # the stack references only catalogue images (+ the allowlist)
#   make -C andai images-push   # push every catalogue image at VERSION to ghcr.io/andai
#
# `make -C andai` runs with cwd=andai/, so every path below is absolute, derived from ROOT.
# Munshi's compose file keeps its own relative paths because --project-directory is deploy/compose.
# Override the env file with ENV_FILE=/abs/path (default: andai/deploy/compose/.env, git-ignored)
# and the compose project with PROJECT=name (default: andai-meet).

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ENV_FILE ?= $(ROOT)/andai/deploy/compose/.env
PROJECT ?= andai-meet
# Images: ghcr.io/andai/munshi-<name>:$(VERSION) for every entry of andai/images.json, built
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
DC = env $(STRIP_ENV) ANDAI_ROOT=$(ROOT) docker compose -p $(PROJECT) --project-directory $(ROOT)/deploy/compose -f $(ROOT)/deploy/compose/docker-compose.yml -f $(ROOT)/andai/deploy/compose/docker-compose.andai.yml --env-file $(ENV_FILE)
# Image variables a shell could export over the env file; images-check must see the template's values.
STRIP_IMAGE_ENV := -u IMAGE_TAG -u BROWSER_IMAGE -u AGENT_IMAGE -u AGENT_WORKER_IMAGE -u DASHBOARD_IMAGE -u COMPOSE_PROFILES
WAIT_TIMEOUT ?= 600
S ?=
# andai-core's database bootstrap (run by psql inside the postgres container).
DB_INIT_SQL := $(ROOT)/andai/services/core/migrations/db-init.sql
# $(call env_value,KEY): shell pipeline printing KEY's last value in ENV_FILE, unquoted. Secrets are
# read into shell variables and passed to containers through the environment, never as arguments.
env_value = grep -E '^$(1)=' "$(ENV_FILE)" | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$$//'

.PHONY: help config config-json config-services up db-init db-migrate bootstrap agent-worker images images-check images-push down ps logs health _env-ready _env-file

help:
	@echo "make -C andai up       gen secrets, build missing catalogue images (PULL=1: pull them), start, wait healthy, db-init, db-migrate"
	@echo "make -C andai db-init | db-migrate                       andai database + roles (idempotent) | migrate to head"
	@echo "make -C andai bootstrap EMAIL=a@b.c ORG=name [NAME=..]   first org + owner (mails a set-password link)"
	@echo "make -C andai images [ONLY=a,b] [PLATFORM=linux/amd64]   build the catalogue (andai/images.json)"
	@echo "make -C andai images-check | images-push                 stack uses only catalogue images | publish"
	@echo "make -C andai health   HTTP health on 127.0.0.1 + container states"
	@echo "make -C andai ps | logs [S=service] | down | config"
	@echo "ENV_FILE=$(ENV_FILE)  PROJECT=$(PROJECT)"

## config — validate the merged compose config (quiet: the merged config contains secrets).
config:
	@test -f "$(ENV_FILE)" || { echo "missing $(ENV_FILE)" >&2; exit 1; }
	$(DC) config --quiet

## config-json / config-services — the merged config exactly as `up` renders it (shell-stripped).
config-json:
	@$(DC) config --format json

config-services:
	@$(DC) config --services

## _env-file — the env file exists.
_env-file:
	@test -f "$(ENV_FILE)" || { echo "missing $(ENV_FILE)" >&2; exit 1; }

## _env-ready — the env file exists, is not the tracked template, and has no OLLM placeholders.
_env-ready:
	@test -f "$(ENV_FILE)" || { echo "missing $(ENV_FILE): cp andai/deploy/compose/.env.example andai/deploy/compose/.env and fill the __OLLM_*__ values" >&2; exit 1; }
	@case "$(ENV_FILE)" in *.example) echo "ENV_FILE must not be the tracked template" >&2; exit 1;; esac
	@if grep -v '^[[:space:]]*#' "$(ENV_FILE)" | grep -q '__OLLM_'; then echo "$(ENV_FILE) still has __OLLM_*__ placeholders; fill them first (andai/docs/dev-vm.md)" >&2; exit 1; fi

## up — the single bring-up command. Every Munshi, bot, agent-worker and andai image is a catalogue
## image built from this repo (andai/images.json). Images the stack needs that are not on this machine
## are built (`images`), or pulled from ghcr.io/andai with PULL=1; nothing else is pulled
## except the allowlisted third-party images (postgres, valkey, versitygw). --no-build at start time.
## Once the stack is healthy, andai-core's database is created (db-init) and migrated (db-migrate).
up: _env-ready
	$(ROOT)/andai/scripts/gen-secrets.sh "$(ENV_FILE)"
	$(DC) config --format json | $(IMAGES) ensure --version $(VERSION) --platform $(PLATFORM) $(if $(filter 1,$(PULL)),--pull)
	$(DC) up -d --no-build --wait --wait-timeout $(WAIT_TIMEOUT)
	$(MAKE) --no-print-directory db-init db-migrate

## db-init — create the `andai` database and the roles andai_owner / andai_app on the running
## postgres service by piping migrations/db-init.sql into psql inside the container (superuser over
## the local socket). Idempotent, and works on an existing volume (no init-script mount). Re-running
## re-applies the passwords from ENV_FILE.
db-init: _env-file
	@set -e; \
	 owner="$$($(call env_value,ANDAI_DB_PASSWORD_OWNER))"; app="$$($(call env_value,ANDAI_DB_PASSWORD_APP))"; \
	 if [ -z "$$owner" ] || [ -z "$$app" ]; then \
	   echo "db-init: ANDAI_DB_PASSWORD_OWNER / ANDAI_DB_PASSWORD_APP are empty in $(ENV_FILE); run andai/scripts/gen-secrets.sh" >&2; exit 1; \
	 fi; \
	 ANDAI_DB_PASSWORD_OWNER="$$owner" ANDAI_DB_PASSWORD_APP="$$app" \
	   $(DC) exec -T -e ANDAI_DB_PASSWORD_OWNER -e ANDAI_DB_PASSWORD_APP postgres \
	   sh -c 'exec psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -f -' < "$(DB_INIT_SQL)"; \
	 echo "db-init: database andai and roles andai_owner, andai_app ready"

## db-migrate — run andai-core's Alembic migrations to head as andai_owner, in a one-shot andai-core
## container. The owner URL goes only to that container; the running service never holds it.
db-migrate: _env-file
	@set -e; \
	 owner="$$($(call env_value,ANDAI_DB_PASSWORD_OWNER))"; \
	 if [ -z "$$owner" ]; then echo "db-migrate: ANDAI_DB_PASSWORD_OWNER is empty in $(ENV_FILE)" >&2; exit 1; fi; \
	 ANDAI_DB_URL_OWNER="postgresql+psycopg://andai_owner:$$owner@postgres:5432/andai" \
	   $(DC) run --rm -T -e ANDAI_DB_URL_OWNER andai-core python -m alembic upgrade head

## bootstrap — create the first org and its owner in the running andai-core (refused once any user
## exists). The owner gets a set-password link by mail (the stack's Mailpit unless SMTP_* say otherwise).
EMAIL ?=
ORG ?=
NAME ?=
bootstrap: _env-file
	@test -n "$(EMAIL)" && test -n "$(ORG)" || { echo "usage: make -C andai bootstrap EMAIL=owner@example.com ORG='Example Ltd' [NAME='A. Owner']" >&2; exit 2; }
	$(DC) exec -T andai-core python -m andai_core bootstrap --email "$(EMAIL)" --org "$(ORG)" $(if $(NAME),--name "$(NAME)")

## images — build every catalogue entry in order (bases first), tagged
## ghcr.io/andai/munshi-<name>:$(VERSION) and :dev; prints size and build time per image.
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
## included; not the :dev or test tags) to ghcr.io/andai. Refuses without a ghcr.io login in
## the docker config, or if any image is missing locally or is not $(PLATFORM). Log in first (the
## token goes straight from gh to docker, never onto the screen):
##   gh auth refresh -h github.com -s write:packages
##   gh auth token | docker login ghcr.io -u "$(gh api user -q .login)" --password-stdin
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
