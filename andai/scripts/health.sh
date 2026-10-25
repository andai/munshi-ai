#!/usr/bin/env bash
# health.sh ENV_FILE [-] — check every service of the andai-meet stack on this host.
#
# 1. HTTP: curls each published health endpoint on 127.0.0.1 (ports read from ENV_FILE, falling
#    back to the compose defaults) and fails on anything but HTTP 200. UI services without a health
#    route (terminal, dashboard) are checked at `/` with redirects followed.
# 2. Containers (only with `-`): reads `docker compose ps -a --format
#    '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}'` lines from stdin and fails on any container
#    that is unhealthy / still starting, not running, or exited non-zero. This covers services with
#    no published HTTP port (postgres, redis, flows-worker, storage-init). andai/Makefile's
#    `health` target pipes that in.
#
# ENV_FILE is parsed as plain KEY=VALUE text; it is never sourced or executed.
set -uo pipefail

[ $# -ge 1 ] || { echo "usage: $0 ENV_FILE [-]" >&2; exit 2; }
env_file="$1"
read_containers="${2:-}"
[ -f "$env_file" ] || { echo "health: $env_file not found" >&2; exit 2; }
command -v curl >/dev/null || { echo "health: curl not found" >&2; exit 2; }

getv() { # getv KEY DEFAULT — last KEY=value in ENV_FILE, else DEFAULT
  local v
  v="$(grep -E "^$1=" "$env_file" | tail -n 1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"
  printf '%s' "${v:-$2}"
}

fail=0
check() { # check NAME PORT PATH [follow]
  local name="$1" url="http://127.0.0.1:$2$3" code
  if [ "${4:-}" = follow ]; then
    code="$(curl -sS -L -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)"
  else
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)"
  fi
  if [ "$code" = 200 ]; then
    printf 'OK    %-14s %s\n' "$name" "$url"
  else
    printf 'FAIL  %-14s %s (HTTP %s)\n' "$name" "$url" "${code:-000}"
    fail=1
  fi
}

echo "== HTTP health (127.0.0.1)"
check gateway      "$(getv API_GATEWAY_HOST_PORT 18056)" /health
check admin-api    "$(getv ADMIN_API_PORT 18057)"        /health
check meeting-api  "$(getv MEETING_API_PORT 18080)"      /health
check runtime      "$(getv RUNTIME_API_PORT 18090)"      /health
check agent-api    "$(getv AGENT_API_PORT 18100)"        /health
check flows-api    "$(getv FLOWS_API_HOST_PORT 18200)"   /health
check mcp          "$(getv MCP_HOST_PORT 18010)"         /health
check storage      "$(getv STORAGE_HOST_PORT 18900)"     /health
check terminal     "$(getv TERMINAL_PORT 13000)"         / follow
case ",$(getv COMPOSE_PROFILES ''),"  in
  *,dashboard,*) check dashboard "$(getv DASHBOARD_PORT 13001)" / follow ;;
esac
check andai-core   "$(getv ANDAI_CORE_PORT 18300)"       /healthz
check andai-worker "$(getv ANDAI_WORKER_PORT 18301)"     /healthz
check andai-intake "$(getv ANDAI_INTAKE_PORT 18302)"     /healthz
check andai-portal "$(getv ANDAI_PORTAL_PORT 13100)"     /api/health

if [ "$read_containers" = - ]; then
  echo "== Containers"
  seen=0
  while IFS='|' read -r svc state health exitcode; do
    [ -n "${svc:-}" ] || continue
    seen=1
    status=OK
    case "$state" in
      running)
        case "$health" in
          ""|healthy) ;;
          *) status=FAIL ;;
        esac ;;
      exited)
        [ "${exitcode:-1}" = 0 ] || status=FAIL ;;
      *) status=FAIL ;;
    esac
    [ "$status" = OK ] || fail=1
    printf '%-5s %-14s state=%s health=%s exit=%s\n' "$status" "$svc" "$state" "${health:--}" "${exitcode:--}"
  done
  if [ "$seen" = 0 ]; then
    echo "FAIL  no containers reported (is the stack up?)"
    fail=1
  fi
fi

if [ "$fail" = 0 ]; then echo "ALL HEALTHY"; else echo "UNHEALTHY"; fi
exit "$fail"
