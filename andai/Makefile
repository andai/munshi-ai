# andai/Makefile — one-command bring-up of Munshi + the andai overlay (PLAT-01).
#
#   make -C andai up        # the single documented bring-up command
#   make -C andai health    # every service healthy?
#
# `make -C andai` runs with cwd=andai/, so every path below is absolute, derived from ROOT.
# Munshi's compose file keeps its own relative paths because --project-directory is deploy/compose.
# Override the env file with ENV_FILE=/abs/path (default: andai/deploy/compose/.env, git-ignored).

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ENV_FILE ?= $(ROOT)/andai/deploy/compose/.env
AGENT_WORKER_IMAGE_TAG ?= andai-meet/andai-agent-worker:dev
# Shell variables override --env-file in Compose interpolation, and Munshi's compose file interpolates
# ANTHROPIC_* / CLAUDE_CODE_OAUTH_TOKEN / HOST_CLAUDE_CREDENTIALS (incl. a credentials bind mount).
# Strip them so a developer shell (e.g. one running Claude Code) can never inject an Anthropic
# endpoint or credential (D-22). The OLLM agent values come only from ANDAI_AGENT_* in ENV_FILE.
STRIP_ENV := -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL -u ANTHROPIC_MODEL -u ANTHROPIC_DEFAULT_OPUS_MODEL -u ANTHROPIC_DEFAULT_SONNET_MODEL -u ANTHROPIC_DEFAULT_HAIKU_MODEL -u CLAUDE_CODE_OAUTH_TOKEN -u HOST_CLAUDE_CREDENTIALS
DC = env $(STRIP_ENV) ANDAI_ROOT=$(ROOT) docker compose -p andai-meet --project-directory $(ROOT)/deploy/compose -f $(ROOT)/deploy/compose/docker-compose.yml -f $(ROOT)/andai/deploy/compose/docker-compose.andai.yml --env-file $(ENV_FILE)
ANDAI_SERVICES := andai-core andai-worker andai-intake andai-portal
WAIT_TIMEOUT ?= 600
S ?=

.PHONY: help config config-json config-services up agent-worker down ps logs health _env-ready

help:
	@echo "make -C andai up       gen secrets, pull Munshi images (+agent-worker, bot), build andai images, start, wait healthy"
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
	@if grep -q '__OLLM_' "$(ENV_FILE)"; then echo "$(ENV_FILE) still has __OLLM_*__ placeholders; fill them first (andai/docs/dev-vm.md)" >&2; exit 1; fi

## up — the single bring-up command. Munshi services run the published v0.12.28 images (never built
## from source here); only the andai images are built. --no-build keeps it that way at start time.
up: _env-ready
	$(ROOT)/andai/scripts/gen-secrets.sh "$(ENV_FILE)"
	$(DC) pull
	$(DC) --profile build-only pull agent-worker
	@bot="$$($(DC) config --format json | python3 -I -c "import json,sys; e=json.load(sys.stdin)['services']['runtime']['environment']; print(e['BROWSER_IMAGE'])")"; \
	 echo "docker pull $$bot"; docker pull "$$bot"
	$(DC) build $(ANDAI_SERVICES)
	$(MAKE) --no-print-directory agent-worker
	$(DC) up -d --no-build --wait --wait-timeout $(WAIT_TIMEOUT)

## agent-worker — the hardened andai agent-worker image (D-22), FROM Munshi's digest-pinned worker.
agent-worker:
	docker build -t $(AGENT_WORKER_IMAGE_TAG) $(ROOT)/andai/deploy/agent-worker

down:
	$(DC) down

ps:
	$(DC) ps -a

logs:
	$(DC) logs --tail=200 $(S)

health:
	@$(DC) ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' | $(ROOT)/andai/scripts/health.sh "$(ENV_FILE)" -
