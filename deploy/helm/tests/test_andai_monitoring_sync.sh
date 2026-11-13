#!/usr/bin/env bash
# The andai chart's monitoring files (charts/andai/files/monitoring) are byte-for-byte copies of the
# Compose monitoring configs (andai/deploy/monitoring): one set of rules, notification templates and
# dashboards for both deployments. Fails on any drift. `--write` copies the sources over the chart's
# copies (make -C deploy/helm andai-sync-monitoring). No helm needed.
set -euo pipefail

HELM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HELM_DIR/../.." && pwd)"
SRC="$ROOT/andai/deploy/monitoring"
DST="$HELM_DIR/charts/andai/files/monitoring"

# chart copy (flat: a ConfigMap key cannot hold a directory) <- source under andai/deploy/monitoring
MAP="
prometheus.yml              prometheus/prometheus.yml
munshi-alerts.yml           prometheus/rules/munshi-alerts.yml
munshi.tmpl                 alertmanager/templates/munshi.tmpl
perses-project.yaml         perses/provisioning/project.yaml
perses-datasource.yaml      perses/provisioning/datasource.yaml
dashboard-joins.yaml        perses/provisioning/dashboards/joins.yaml
dashboard-asr.yaml          perses/provisioning/dashboards/asr.yaml
dashboard-gpu.yaml          perses/provisioning/dashboards/gpu.yaml
dashboard-pipeline.yaml     perses/provisioning/dashboards/pipeline.yaml
dashboard-intake.yaml       perses/provisioning/dashboards/intake.yaml
dashboard-email.yaml        perses/provisioning/dashboards/email.yaml
"

write=0
[ "${1:-}" = "--write" ] && write=1

echo "=== andai chart monitoring files vs andai/deploy/monitoring ==="
fail=0
n=0
while read -r copy source; do
  [ -n "$copy" ] || continue
  n=$((n + 1))
  if [ ! -f "$SRC/$source" ]; then echo "  FAIL: source missing: andai/deploy/monitoring/$source"; fail=1; continue; fi
  if [ "$write" -eq 1 ]; then
    mkdir -p "$DST"
    cp "$SRC/$source" "$DST/$copy"
    echo "  wrote: files/monitoring/$copy <- $source"
  elif [ ! -f "$DST/$copy" ]; then
    echo "  FAIL: files/monitoring/$copy missing (make -C deploy/helm andai-sync-monitoring)"; fail=1
  elif cmp -s "$SRC/$source" "$DST/$copy"; then
    echo "  OK: files/monitoring/$copy == $source"
  else
    echo "  FAIL: files/monitoring/$copy differs from $source (make -C deploy/helm andai-sync-monitoring)"; fail=1
  fi
done <<< "$MAP"

# Every dashboard the Compose profile provisions is copied (a new one must be added to MAP).
want="$(find "$SRC/perses/provisioning/dashboards" -name '*.yaml' | wc -l | tr -d ' ')"
got="$(printf '%s\n' "$MAP" | grep -c '^dashboard-' || true)"
if [ "$want" = "$got" ]; then echo "  OK: $got dashboards mapped"; else echo "  FAIL: $want dashboards in the source, $got mapped"; fail=1; fi

# No stray file in the chart copy (README.md documents the directory).
for f in "$DST"/*; do
  name="$(basename "$f")"
  [ "$name" = "README.md" ] && continue
  printf '%s\n' "$MAP" | awk '{print $1}' | grep -qx "$name" || { echo "  FAIL: files/monitoring/$name is not a mapped copy"; fail=1; }
done

[ "$fail" -eq 0 ] && { echo "andai monitoring sync: PASS ($n files)"; exit 0; } || { echo "andai monitoring sync: FAIL"; exit 1; }
