#!/usr/bin/env bash
# Structure + lint of the andai umbrella chart (no cluster). The dependencies are vendored first
# (make andai-deps: the pinned subcharts once, the local munshi chart every time). With the chart's
# defaults the render must stop on the KEK Secret; values-test, values-staging and values-loadtest
# (on top of staging) must lint clean. Skips when helm is absent.
set -euo pipefail

HELM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHART="$HELM_DIR/charts/andai"

echo "=== Helm chart structure (andai umbrella) ==="
for f in Chart.yaml Chart.lock values.yaml values-test.yaml values-staging.yaml values-loadtest.yaml templates files/monitoring; do
  [ -e "$CHART/$f" ] || { echo "FAIL: missing $f"; exit 1; }
  echo "  OK: $f"
done

if ! command -v helm >/dev/null 2>&1; then
  echo "  SKIP: helm not installed"; echo "andai chart lint: SKIP"; exit 0
fi

make -s -C "$HELM_DIR" andai-deps
helm dependency list "$CHART"
if helm dependency list "$CHART" | awk 'NR > 1 && NF && $4 != "ok" { bad = 1 } END { exit !bad }'; then
  echo "FAIL: a dependency is not vendored at its pinned version"; exit 1
fi

# The versions in Chart.yaml, Chart.lock, the root VERSION and the image tag agree.
app="$(awk -F'"' '/^appVersion:/ {print $2}' "$CHART/Chart.yaml")"
tag="$(awk '/^global:/ {g=1} g && /imageTag:/ {gsub(/"/, "", $2); print $2; exit}' "$CHART/values.yaml")"
ver="$(tr -d '[:space:]' < "$HELM_DIR/../../VERSION")"
if [ "$app" = "$ver" ] && [ "$tag" = "$ver" ]; then
  echo "  OK: appVersion, global.imageTag and VERSION agree ($ver)"
else
  echo "FAIL: appVersion=$app global.imageTag=$tag VERSION=$ver must agree"; exit 1
fi

# helm lint reports a template's `fail` as [INFO] and lints the rest; helm template (and install)
# stop on it. With the defaults the first refusal is the KEK Secret's.
echo "  Linting (defaults: the KEK Secret refusal)..."
out="$(helm lint "$CHART" 2>&1)" || { echo "$out"; echo "FAIL: helm lint with the defaults"; exit 1; }
if grep -q 'Fail: andai.kek.existingSecret is required' <<< "$out"; then
  echo "  OK: helm lint reports the KEK Secret refusal"
else
  echo "$out"; echo "FAIL: helm lint with the defaults does not report the KEK Secret refusal"; exit 1
fi
if out="$(helm template andai "$CHART" -n munshi 2>&1)"; then
  echo "FAIL: helm template rendered with the defaults; it must refuse without andai.kek.existingSecret"; exit 1
fi
if grep -q 'andai.kek.existingSecret is required' <<< "$out"; then
  echo "  OK: helm template refuses the defaults on the KEK Secret"
else
  echo "$out"; echo "FAIL: helm template refused the defaults, but not on the KEK Secret"; exit 1
fi

# A values file lints clean only if helm lint reports no refusal and helm template renders it.
lint_clean() {  # lint_clean <label> <helm args...>
  local label="$1"; shift
  echo "  Linting ($label)..."
  local out
  out="$(helm lint "$CHART" "$@" 2>&1)" || { echo "$out"; echo "FAIL: helm lint ($label)"; exit 1; }
  if grep -qE 'Fail:|\[ERROR\]|\[WARNING\]' <<< "$out"; then echo "$out"; echo "FAIL: helm lint ($label) reported a refusal or warning"; exit 1; fi
  helm template andai "$CHART" -n munshi "$@" > /dev/null || { echo "FAIL: helm template ($label)"; exit 1; }
  echo "  OK: $label lints and renders"
}
lint_clean "values-test" -f "$CHART/values-test.yaml"
lint_clean "values-test, monitoring on" -f "$CHART/values-test.yaml" --set monitoring.enabled=true
# The Postgres superuser password is never in a tracked file: staging passes it from an untracked
# file (--set-file); the lint stands in a placeholder.
lint_clean "values-staging" -f "$CHART/values-staging.yaml" --set munshi.database.password=helm-lint-only
lint_clean "values-staging + values-loadtest" -f "$CHART/values-staging.yaml" -f "$CHART/values-loadtest.yaml" \
  --set munshi.database.password=helm-lint-only --set munshi.runtime.maxActiveBots=12

echo "andai chart lint: PASS"
