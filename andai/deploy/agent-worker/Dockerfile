# andai hardened agent worker (D-22).
#
# Munshi v0.12.28's agent plane runs every chat / routine / post-meeting turn through the `claude-code`
# harness (the claude CLI) inside this worker image. andai points that CLI at the OLLM endpoint's
# Anthropic-compatible API (ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL, forwarded by
# the runtime). This image adds only environment defaults on top of Munshi's published worker:
#
#   - the CLI's non-model traffic (telemetry, error reporting, auto-update) is switched off;
#   - chat turns get workspace tools only — no Bash, WebSearch or WebFetch (Munshi's default set),
#     so a turn cannot reach the internet or run shell commands;
#   - ANTHROPIC_BASE_URL defaults to an unroutable host, so a worker spawned without the OLLM value
#     fails closed instead of falling back to api.anthropic.com. The runtime's spawn env overrides it.
#
# The runtime uses this image because AGENT_WORKER_IMAGE (andai/deploy/compose/.env) names it.
FROM ghcr.io/sunydepalpur/munshi-agent-worker:v0.12.28

ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_AUTOUPDATER=1 \
    MUNSHI_CHAT_TOOLS=Read,Write,Edit,Glob,Grep \
    ANTHROPIC_BASE_URL=http://ollm-not-configured.invalid
