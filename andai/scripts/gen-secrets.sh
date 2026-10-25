#!/usr/bin/env bash
# gen-secrets.sh ENV_FILE — fill the stack secrets in an andai .env file.
#
# For each key below, writes a fresh `openssl rand -hex 32` value when the key is missing, empty,
# or set to a published placeholder (Munshi's .env.example dev values, or a value Munshi's services
# refuse by name). A key that already holds a real value is left alone, so re-running is safe and
# never rotates a secret a running stack depends on. Values are never printed.
#
# Keys: ADMIN_TOKEN, INTERNAL_API_SECRET, MUNSHI_DISPATCH_SIGNING_KEY, NEXTAUTH_SECRET, plus
# MUNSHI_FLOWS_API_KEY and MUNSHI_FLOWS_TIMELINE_KEY (Munshi's flows services refuse to boot without
# them; Munshi's own deploy/compose/mint-dev-env.sh mints the same two).
set -euo pipefail

usage() { echo "usage: $0 ENV_FILE" >&2; exit 2; }
[ $# -eq 1 ] || usage
env_file="$1"

if [ ! -f "$env_file" ]; then
  echo "gen-secrets: $env_file does not exist." >&2
  echo "  cp andai/deploy/compose/.env.example andai/deploy/compose/.env, fill the __OLLM_*__ values, retry." >&2
  exit 1
fi
case "$(basename "$env_file")" in
  .env.example|*.example)
    echo "gen-secrets: refusing to write secrets into the tracked template $env_file" >&2
    exit 1 ;;
esac
command -v openssl >/dev/null || { echo "gen-secrets: openssl not found" >&2; exit 1; }

keys=(ADMIN_TOKEN INTERNAL_API_SECRET MUNSHI_DISPATCH_SIGNING_KEY NEXTAUTH_SECRET MUNSHI_FLOWS_API_KEY MUNSHI_FLOWS_TIMELINE_KEY)
placeholders=" dev-admin-token dev-dispatch-signing-key dev-nextauth-secret munshi-internal-secret lite-internal-secret changeme change-me CHANGE-ME default secret "

tmp="$(mktemp "${env_file}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cp "$env_file" "$tmp"

for key in "${keys[@]}"; do
  # Last assignment wins in dotenv, so judge the last one.
  current="$(grep -E "^${key}=" "$tmp" | tail -n 1 | cut -d= -f2- || true)"
  current="${current%\"}"; current="${current#\"}"
  if [ -n "$current" ] && [[ "$placeholders" != *" $current "* ]]; then
    echo "kept    $key"
    continue
  fi
  value="$(openssl rand -hex 32)"
  if grep -qE "^${key}=" "$tmp"; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$tmp" > "$tmp.new"
    mv "$tmp.new" "$tmp"
    echo "minted  $key"
  else
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    echo "added   $key"
  fi
done

chmod 600 "$tmp"
mv "$tmp" "$env_file"
trap - EXIT
