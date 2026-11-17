#!/usr/bin/env bash
# The mock bot image starts from pinned dependencies, with no network and no npm call.
#
#   1. the entrypoint execs `node --import tsx …/mock/main.ts` and never calls npm or npx (npx runs
#      npm's update check, which is how every mock start reached the npm registry on staging);
#   2. /app/package.json pins exact versions: the four direct dependencies equal what the root
#      pnpm-lock.yaml resolves for core/meetings/services/bot, and every override is a lockfile
#      version;
#   3. every package installed under /app/node_modules is a version pnpm-lock.yaml records;
#   4. a container with `--network none` starts the mock: its first log line is `[mock] scenario=…`.
#
# Builds the image from Dockerfile.mock (repo root context) unless MOCK_IMAGE names an existing one.
# Never pulls: the build uses the local base image and every `docker run` is --pull never.
#
#   bash deploy/compose/tests/mock_image_start_test.sh                  # build, then check
#   MOCK_IMAGE=ghcr.io/andai/munshi-mock-bot:dev bash deploy/compose/tests/mock_image_start_test.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PLATFORM="${PLATFORM:-linux/amd64}"
IMAGE="${MOCK_IMAGE:-}"
BUILT=""
CID=""
FAILED=0

cleanup() {
  if [ -n "$CID" ]; then docker rm -f "$CID" >/dev/null 2>&1 || true; fi
  if [ -n "$BUILT" ]; then docker rmi "$BUILT" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

ok() { echo "  ok   $1"; }
bad() { echo "  FAIL $1" >&2; FAILED=1; }

if [ -z "$IMAGE" ]; then
  IMAGE="mock-bot-start-test:local"
  BUILT="$IMAGE"
  echo "building $IMAGE ($PLATFORM) from core/meetings/services/bot/Dockerfile.mock"
  docker buildx build --platform "$PLATFORM" --load -t "$IMAGE" \
    -f "$ROOT/core/meetings/services/bot/Dockerfile.mock" "$ROOT" >/dev/null
fi

in_image() { docker run --rm --pull never --network none --platform "$PLATFORM" --entrypoint "$1" "$IMAGE" "${@:2}"; }

# ── 1. the entrypoint ────────────────────────────────────────────────────────────────────────────
ENTRYPOINT="$(in_image cat /app/entrypoint.sh)"
if grep -qx 'exec node --import tsx /app/meetings/services/bot/mock/main.ts' <<<"$ENTRYPOINT"; then
  ok "entrypoint execs node --import tsx"
else
  bad "entrypoint does not exec 'node --import tsx …/mock/main.ts':"$'\n'"$ENTRYPOINT"
fi
if grep -Eq '(^|[^A-Za-z0-9_-])(npm|npx)([^A-Za-z0-9_-]|$)' <<<"$ENTRYPOINT"; then
  bad "entrypoint calls npm or npx"
else
  ok "entrypoint calls neither npm nor npx"
fi

# ── 2 and 3. pinned versions, checked against the root lockfile ──────────────────────────────────
MANIFEST="$(in_image cat /app/package.json)"
INSTALLED="$(in_image node -e '
const fs = require("fs"), path = require("path");
const out = [];
const visit = (dir) => {
  if (!fs.existsSync(dir)) return;
  for (const name of fs.readdirSync(dir)) {
    if (name.startsWith(".")) continue;
    const full = path.join(dir, name);
    if (name.startsWith("@")) { visit(full); continue; }
    const pj = path.join(full, "package.json");
    if (fs.existsSync(pj)) {
      const p = JSON.parse(fs.readFileSync(pj, "utf8"));
      out.push(`${p.name}@${p.version}`);
      visit(path.join(full, "node_modules"));
    }
  }
};
visit("/app/node_modules");
console.log(out.join("\n"));
')"
MANIFEST="$MANIFEST" INSTALLED="$INSTALLED" python3 -I - "$ROOT/pnpm-lock.yaml" <<'PY' || FAILED=1
import json, os, re, sys

lock = open(sys.argv[1], encoding="utf-8").read()
manifest = json.loads(os.environ["MANIFEST"])
installed = [l for l in os.environ["INSTALLED"].splitlines() if l.strip()]
failed = False

def say(good, msg):
    global failed
    print(("  ok   " if good else "  FAIL ") + msg, file=sys.stdout if good else sys.stderr)
    failed |= not good

# What pnpm resolves for the bot package: `      <name>:\n        specifier: …\n        version: <v>`.
importer = re.search(r"^  core/meetings/services/bot:\n(.*?)(?=^  \S)", lock, re.M | re.S).group(1)
resolved = {
    m.group(1).strip("'"): m.group(2).split("(")[0]
    for m in re.finditer(r"^      ('?[^:\n]+'?):\n        specifier: .*\n        version: (\S+)$", importer, re.M)
}
# Every package version the lockfile records (`  name@1.2.3:` or `  '@scope/name@1.2.3':`).
packages = lock.split("\nsnapshots:\n")[0]
recorded = set(re.findall(r"^  '?((?:@[^/\s]+/)?[^@\s']+@[^:'(\s]+)'?:", packages, re.M))

exact = re.compile(r"^\d+\.\d+\.\d+$")
direct = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
say(set(direct) == {"ajv", "ajv-formats", "redis", "tsx"},
    f"direct dependencies are ajv, ajv-formats, redis, tsx ({', '.join(sorted(direct))})")
for name, version in sorted(direct.items()):
    say(bool(exact.match(version)) and version == resolved.get(name),
        f"{name} {version} == pnpm-lock.yaml's {resolved.get(name)} for core/meetings/services/bot")
for name, version in sorted(manifest.get("overrides", {}).items()):
    say(bool(exact.match(version)) and f"{name}@{version}" in recorded,
        f"override {name} {version} is a pnpm-lock.yaml version")
say(bool(installed), f"{len(installed)} packages installed under /app/node_modules")
strays = [p for p in installed if p not in recorded]
say(not strays, "every installed package is a pnpm-lock.yaml version"
    + (f" — not in the lockfile: {', '.join(strays)}" if strays else ""))
sys.exit(1 if failed else 0)
PY

# ── 4. a start with no network ───────────────────────────────────────────────────────────────────
CONFIG='{"platform":"google_meet","meetingUrl":"https://meet.google.com/abc-defg-hij","botName":"mock:hold","redisUrl":"redis://127.0.0.1:6379","connectionId":"start-test","nativeMeetingId":"abc-defg-hij"}'
CID="$(docker run -d --pull never --network none --platform "$PLATFORM" \
  -e MUNSHI_BOT_CONFIG="$CONFIG" "$IMAGE")"
FIRST=""
for _ in $(seq 1 90); do
  FIRST="$(docker logs "$CID" 2>&1 | head -n 1 || true)"
  [ -n "$FIRST" ] && break
  sleep 1
done
if [[ "$FIRST" == "[mock] scenario=hold "* ]]; then
  ok "started with --network none: $FIRST"
else
  bad "no '[mock] scenario=hold' first log line with --network none (got: ${FIRST:-nothing})"
  docker logs "$CID" 2>&1 | head -n 20 >&2 || true
fi
if docker logs "$CID" 2>&1 | grep -Eiq 'npm (warn|err|notice)|registry\.npmjs'; then
  bad "the container's log mentions npm"
else
  ok "the container's log never mentions npm"
fi

if [ "$FAILED" -ne 0 ]; then
  echo "mock_image_start_test: FAIL ($IMAGE)" >&2
  exit 1
fi
echo "mock_image_start_test: PASS ($IMAGE)"
