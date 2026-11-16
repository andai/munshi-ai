#!/usr/bin/env bash
# Render the andai umbrella chart (no cluster) in each configuration and assert what it must and must
# not contain: the four andai Deployments, the migrations hook, the KEK as a read-only Secret mount,
# SeaweedFS (SSE-S3, telemetry off) or a bring-your-own bucket, disjoint GPU pools, the bot pool and
# bot sizing, opt-in monitoring with receivers off and URLs only in Secrets, no Grafana, and only
# catalogue images. The render-time refusals are checked too. Skips when helm is absent.
set -euo pipefail

HELM_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HELM_DIR/../.." && pwd)"
CHART="$HELM_DIR/charts/andai"

if ! command -v helm >/dev/null 2>&1; then
  echo "SKIP: helm not installed"; exit 0
fi
make -s -C "$HELM_DIR" andai-deps

# A YAML parser for the structural checks: the system python3 if it has PyYAML, else the andai uv
# workspace (which pins it).
if python3 -I -c 'import yaml' 2>/dev/null; then PY=(python3 -I); else PY=(uv run --quiet --directory "$ROOT/andai" python -I); fi

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
TEST=(-f "$CHART/values-test.yaml")
STAGING=(-f "$CHART/values-staging.yaml" --set munshi.database.password=helm-render-test-only)

render() {  # render <name> <helm args...> — writes $OUT/<name>.yaml or fails the test
  local name="$1"; shift
  helm template andai "$CHART" -n munshi "$@" > "$OUT/$name.yaml" 2> "$OUT/$name.err" \
    || { cat "$OUT/$name.err"; echo "FAIL: render $name"; exit 1; }
}
render test "${TEST[@]}"
render mon "${TEST[@]}" --set monitoring.enabled=true
render receivers "${TEST[@]}" --set monitoring.enabled=true --set monitoring.alerting.email.enabled=true \
  --set monitoring.alerting.slack.enabled=true --set monitoring.alerting.teams.enabled=true
render gpumetrics "${TEST[@]}" --set monitoring.enabled=true --set gpuMetrics.enabled=true \
  --set gpuMetrics.image=dcgm-exporter.example.invalid/dcgm-exporter:test
render gpu "${TEST[@]}" --set gpuPools.asr.enabled=true --set gpuPools.llm.enabled=true \
  --set gpuPools.llm.image=llm.example.invalid/server:test \
  --set munshi.meetingApi.transcriptionServiceUrl=http://andai-asr:8000
render byo "${TEST[@]}" --set seaweedfs.enabled=false --set munshi.storage.s3.endpoint=https://s3.example.test \
  --set munshi.storage.s3.bucket=recordings --set munshi.storage.s3.existingSecret=byo-s3
render byopg "${TEST[@]}" --set munshi.postgres.enabled=false --set munshi.database.host=pg.example.test \
  --set munshi.postgres.credentialsSecretName=pg-admin --set munshi.database.password=
render staging "${STAGING[@]}"
render staginggpu "${STAGING[@]}" --set gpuPools.asr.enabled=true --set gpuPools.llm.enabled=true \
  --set gpuPools.llm.image=llm.example.invalid/server:test \
  --set munshi.meetingApi.transcriptionServiceUrl=http://andai-asr:8000
render loadtest "${STAGING[@]}" -f "$CHART/values-loadtest.yaml" --set munshi.runtime.maxActiveBots=37

# Refusals: each render must fail, and with its own message.
fail=0
refuse() {  # refuse <label> <expected message part> <helm args...>
  local label="$1" want="$2"; shift 2
  local out
  if out="$(helm template andai "$CHART" -n munshi "$@" 2>&1)"; then
    echo "  FAIL: $label rendered; it must be refused"; fail=1
  elif grep -qF -- "$want" <<< "$out"; then
    echo "  OK: refused — $label"
  else
    echo "  FAIL: $label refused without \"$want\":"; echo "$out" | tail -3; fail=1
  fi
}
echo "=== andai chart — render-time refusals ==="
refuse "defaults (no KEK Secret)" "andai.kek.existingSecret is required"
refuse "LLM pool enabled without an image" "gpuPools.llm.image is required" "${TEST[@]}" --set gpuPools.llm.enabled=true
refuse "ASR pool without munshi's transcription URL on it" "transcriptionServiceUrl must be http://andai-asr:8000" \
  "${TEST[@]}" --set gpuPools.asr.enabled=true
refuse "dcgm-exporter without an image" "gpuMetrics.image is required" "${TEST[@]}" --set gpuMetrics.enabled=true
refuse "a secret key in andai.env" "andai.env.SMTP_PASSWORD is refused" "${TEST[@]}" --set andai.env.SMTP_PASSWORD=x
refuse "SeaweedFS without -telemetry=false" "-telemetry=false" "${TEST[@]}" --set-json 'seaweedfs.master.extraArgs=[]'
refuse "SeaweedFS without the SSE-S3 key" "WEED_S3_SSE_KEK" "${TEST[@]}" \
  --set-json 'seaweedfs.filer.secretExtraEnvironmentVars={"WEED_S3_SSE_KEK":{"secretKeyRef":{"name":"","key":""}}}'
refuse "S3 endpoint not the SeaweedFS Service" "munshi.storage.s3.endpoint must be http://seaweedfs-s3:8333" \
  "${TEST[@]}" --set munshi.storage.s3.endpoint=https://s3.example.test
refuse "BYO bucket still pointing at SeaweedFS" "seaweedfs.enabled=false" "${TEST[@]}" --set seaweedfs.enabled=false
refuse "GPU pools on the same nodes" "must be disjoint" "${TEST[@]}" --set gpuPools.asr.enabled=true \
  --set gpuPools.llm.enabled=true --set gpuPools.llm.image=x.invalid/llm:1 \
  --set munshi.meetingApi.transcriptionServiceUrl=http://andai-asr:8000 --set gpuPools.llm.nodeSelector.munshi\\.ai/gpu-pool=asr
refuse "bot pool on GPU nodes" "bot Pods must not land on GPU nodes" "${TEST[@]}" \
  --set munshi.runtime.nodeSelector.munshi\\.ai/gpu-pool=asr
refuse "the munshi default Postgres password" "munshi.database.password is required" "${TEST[@]}" \
  --set munshi.database.password=postgres
refuse "Slack on without the alert Secret mounted" "extraSecretMounts[name=alerting-secrets]" "${TEST[@]}" \
  --set monitoring.enabled=true --set monitoring.alerting.slack.enabled=true --set monitoring.alerting.existingSecret=other
refuse "no portal origin" "andai.portal.origin is required" "${TEST[@]}" --set andai.portal.origin=

echo "=== andai chart — render assertions ==="
"${PY[@]}" - "$OUT" "$ROOT" <<'PY' || fail=1
import json, re, sys
from pathlib import Path

import yaml

out, root = Path(sys.argv[1]), Path(sys.argv[2])
failures = 0


def check(ok, label):
    global failures
    print(f"  {'OK' if ok else 'FAIL'}: {label}")
    if not ok:
        failures += 1


def load(name):
    docs = [d for d in yaml.safe_load_all((out / f"{name}.yaml").read_text()) if d]
    return docs, (out / f"{name}.yaml").read_text()


def objs(docs, kind=None, name=None):
    return [d for d in docs if (kind is None or d["kind"] == kind)
            and (name is None or d["metadata"]["name"] == name)]


def one(docs, kind, name):
    found = objs(docs, kind, name)
    return found[0] if len(found) == 1 else None


def pod_spec(o):
    if o["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        return o["spec"]["template"]["spec"]
    return None


def containers(o, init=True):
    spec = pod_spec(o) or {}
    return list(spec.get("containers", [])) + (list(spec.get("initContainers", [])) if init else [])


def env_of(container):
    return {e["name"]: e for e in container.get("env", []) or []}


def env_value(docs, kind, name, container, var):
    o = one(docs, kind, name)
    if not o:
        return None
    for c in containers(o):
        if c["name"] == container:
            e = env_of(c).get(var)
            return None if e is None else e.get("value", e.get("valueFrom"))
    return None


def images(docs):
    return {c["image"] for o in docs for c in containers(o)}


catalogue = json.loads((root / "andai" / "images.json").read_text())["third_party"]


def catalogued(image):
    bare = image.removeprefix("docker.io/")
    return image.startswith("ghcr.io/andai/munshi-") or bare in catalogue


def no_grafana(docs, text, label):
    hit = [o["kind"] + "/" + o["metadata"]["name"] for o in docs
           if "grafana" in (o["kind"] + o["metadata"]["name"]).lower()]
    hit += [i for i in images(docs) if "grafana" in i.lower()]
    check(not hit, f"{label}: no Grafana object or image ({hit or 'none'})")


WEBHOOK_HOSTS = re.compile(r"hooks\.slack\.com|logic\.azure\.com|webhook\.office\.com|outlook\.office\.com")

# ── values-test: the platform and the andai services ───────────────────────────────────────────
docs, text = load("test")
deploys = {o["metadata"]["name"] for o in objs(docs, "Deployment")}
check({"andai-core", "andai-worker", "andai-intake", "andai-portal"} <= deploys,
      "Deployments andai-core, andai-worker, andai-intake, andai-portal (fixed names)")
for name in ("andai-core", "andai-worker", "andai-intake"):
    svc = one(docs, "Service", name)
    check(svc is not None and svc["spec"]["type"] == "ClusterIP" and svc["spec"]["ports"][0]["port"] == 8000,
          f"Service {name}: ClusterIP on 8000 (Prometheus target {name}:8000)")
check(one(docs, "Service", "andai-intake-smtp") is not None
      and one(docs, "Service", "andai-intake-smtp")["spec"]["ports"][0]["targetPort"] == "smtp",
      "SMTP on its own Service (andai-intake-smtp → 2525)")

job = one(docs, "Job", "andai-migrations")
hooks = (job or {}).get("metadata", {}).get("annotations", {}).get("helm.sh/hook", "")
check(job is not None and set(hooks.split(",")) == {"post-install", "pre-upgrade"},
      f"migrations Job is a hook: pre-upgrade, and post-install with the bundled Postgres ({hooks})")
if job:
    spec = pod_spec(job)
    init = {c["name"]: c for c in spec.get("initContainers", [])}
    main = {c["name"]: c for c in spec["containers"]}
    dbinit = init.get("db-init", {})
    check("psql -q -v ON_ERROR_STOP=1 -f /work/db-init.sql" in " ".join(dbinit.get("command", [])),
          "migrations: db-init runs db-init.sql with psql first")
    pw = env_of(dbinit).get("PGPASSWORD", {})
    check("secretKeyRef" in pw.get("valueFrom", {}) and pw["valueFrom"]["secretKeyRef"]["name"] == "postgres-credentials",
          "migrations: db-init connects as the Postgres superuser from its Secret")
    check(main.get("alembic", {}).get("command") == ["python", "-m", "alembic", "upgrade", "head"],
          "migrations: then alembic upgrade head")
    url = env_of(main.get("alembic", {})).get("ANDAI_DB_URL_OWNER", {}).get("value", "")
    check(url.startswith("postgresql+psycopg://andai_owner:$(ANDAI_DB_PASSWORD_OWNER)@"),
          "migrations: Alembic as andai_owner, password from the Secret (no literal)")

docs_byopg, _ = load("byopg")
hooks = one(docs_byopg, "Job", "andai-migrations")["metadata"]["annotations"]["helm.sh/hook"]
check(set(hooks.split(",")) == {"pre-install", "pre-upgrade"},
      f"migrations Job is pre-install,pre-upgrade with an external Postgres ({hooks})")

for name in ("andai-core", "andai-worker"):
    o = one(docs, "Deployment", name)
    c = containers(o)[0]
    env = env_of(c)
    check(env.get("ANDAI_KEK_FILE", {}).get("value") == "/run/secrets/andai-kek/active"
          and env.get("ANDAI_KEK_RETIRED_FILE", {}).get("value") == "/run/secrets/andai-kek/retired",
          f"{name}: ANDAI_KEK_FILE and ANDAI_KEK_RETIRED_FILE point at the mounted files")
    mount = [m for m in c.get("volumeMounts", []) if m["name"] == "andai-kek"]
    check(mount and mount[0]["mountPath"] == "/run/secrets/andai-kek" and mount[0].get("readOnly") is True,
          f"{name}: KEK Secret mounted read-only at /run/secrets/andai-kek")
    vol = [v for v in pod_spec(o)["volumes"] if v["name"] == "andai-kek"]
    items = sorted(i["key"] for i in vol[0]["secret"].get("items", [])) if vol else []
    check(vol and vol[0]["secret"]["secretName"] == "andai-kek" and items == ["active", "retired"],
          f"{name}: KEK volume is the operator's Secret andai-kek with both files")
    url = env.get("ANDAI_DB_URL_APP", {}).get("value", "")
    check(url.startswith("postgresql+psycopg://andai_app:$(ANDAI_DB_PASSWORD_APP)@"),
          f"{name}: app-role URL takes its password from the Secret")
check(not any(e for o in docs for c in containers(o) for e in (c.get("env") or []) if e["name"] == "ANDAI_KEK"),
      "no ANDAI_KEK env anywhere (the KEK is only a mounted file)")
check(not objs(docs, "Secret", "andai-kek") and not re.search(r"(?m)^\s+(active|ANDAI_KEK):\s", text),
      "no KEK Secret or KEK literal in the render")
intake = one(docs, "Deployment", "andai-intake")
check(not [v for v in pod_spec(intake).get("volumes", []) or [] if v["name"] == "andai-kek"],
      "andai-intake holds no KEK")

ingresses = objs(docs, "Ingress")
backends = {p["backend"]["service"]["name"] for i in ingresses for r in i["spec"]["rules"] for p in r["http"]["paths"]}
check(backends == {"andai-portal"}, f"the only Ingress backend is the portal ({sorted(backends)})")

masters = [o for o in objs(docs, "StatefulSet") if o["metadata"]["name"] == "seaweedfs-master"]
check(masters and all("-telemetry=false" in " ".join(c.get("args", []) + c.get("command", []))
                      for m in masters for c in containers(m, init=False)),
      "SeaweedFS: every master runs with -telemetry=false")
check(env_value(docs, "Deployment", "andai-munshi-meeting-api", "meeting-api", "S3_ENDPOINT") == "http://seaweedfs-s3:8333",
      "munshi meeting-api stores recordings at the SeaweedFS S3 Service (andai.s3Endpoint)")
s3svc = one(docs, "Service", "seaweedfs-s3")
check(s3svc is not None and any(p["port"] == 8333 for p in s3svc["spec"]["ports"]), "SeaweedFS S3 Service on 8333")
filer = one(docs, "StatefulSet", "seaweedfs-filer")
kek = env_of(containers(filer)[0]).get("WEED_S3_SSE_KEK", {}) if filer else {}
check(kek.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == "andai-seaweedfs-sse",
      "SeaweedFS SSE-S3 key from the operator's Secret (WEED_S3_SSE_KEK)")
filer_cmd = " ".join(c for x in containers(filer, init=False) for c in x.get("command", []) + x.get("args", []))
check("-s3.config=/etc/sw/seaweedfs_s3_config" in filer_cmd, "SeaweedFS S3 with auth from the identities Secret")
bucket = one(docs, "Job", "andai-seaweedfs-bucket")
bscript = " ".join(containers(bucket)[0]["command"]) if bucket else ""
check(bucket is not None and "put_bucket_encryption" in bscript and '"SSEAlgorithm": "AES256"' in bscript
      and "post-install" in bucket["metadata"]["annotations"]["helm.sh/hook"],
      "bucket Job (post-install) sets the bucket's default encryption to SSE-S3 (AES256)")
check(not any(o["kind"] == "ClusterRole" and "seaweedfs" in o["metadata"]["name"] for o in docs),
      "SeaweedFS renders no ClusterRole")

rt = "andai-munshi-runtime"
check(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_K8S_SHM_SIZE") == "2Gi", "bot Pods: RUNTIME_K8S_SHM_SIZE=2Gi")
check(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_BOT_MEMORY_MB") == "2560", "bot Pods: RUNTIME_BOT_MEMORY_MB=2560")
check(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_MAX_ACTIVE_BOTS") == "4", "cluster bot cap from values (4)")
check(env_value(docs, "Deployment", "andai-munshi-meeting-api", "meeting-api", "MUNSHI_SYSTEM_WEBHOOK_URL")
      == "http://andai-core:8000/internal/platform-events", "meeting-api posts platform events to andai-core")

state = one(docs, "ConfigMap", "andai-alerting-state")
check(state is not None and json.loads(state["data"]["state.json"]) == {"receivers": 0, "names": []},
      "receivers state file says receivers: 0 by default")
w = one(docs, "Deployment", "andai-worker")
wenv = env_of(containers(w)[0])
wmount = [m for m in containers(w)[0]["volumeMounts"] if m["name"] == "alerting-state"]
check(wenv.get("ANDAI_ALERTING_STATE_FILE", {}).get("value") == "/run/munshi-alerting/state.json"
      and wmount and wmount[0]["mountPath"] == "/run/munshi-alerting"
      and [v for v in pod_spec(w)["volumes"] if v["name"] == "alerting-state"][0]["configMap"]["name"] == "andai-alerting-state",
      "state file mounted into andai-worker at /run/munshi-alerting/state.json")
check(wenv.get("ANDAI_MONITORING", {}).get("value") == "0", "ANDAI_MONITORING=0 while monitoring is off")
check(not objs(docs, "DaemonSet", "dcgm-exporter") and not any("prometheus" in i for i in images(docs)),
      "no dcgm-exporter and no monitoring by default")
cm = one(docs, "ConfigMap", "andai-config")
check(cm is not None and not any(k in cm["data"] for k in ("ANDAI_KEK", "SMTP_PASSWORD", "ANDAI_LLM_API_KEY", "PLATFORM_ADMIN_TOKEN")),
      "andai-config carries no secret")
no_grafana(docs, text, "values-test")
bad = sorted(i for i in images(docs) if not catalogued(i))
check(not bad, f"values-test: every image is a catalogue image or on the third_party list ({bad or 'all'})")

# ── monitoring on ──────────────────────────────────────────────────────────────────────────────
docs, text = load("mon")
prom = [c["image"] for o in objs(docs, "Deployment", "prometheus") for c in containers(o) if c["name"] == "prometheus-server"]
check(prom and re.search(r":v3\.13\.\d+(@sha256:[0-9a-f]{64})?$", prom[0]) is not None, f"Prometheus on the 3.13 LTS line ({prom})")
am = one(docs, "StatefulSet", "alertmanager")
amc = [c for c in containers(am, init=False) if c["name"] == "alertmanager"][0]
check("--config.file=/etc/alertmanager/munshi/alertmanager.yml" in amc["args"],
      "Alertmanager reads its config from the mounted Secret")
vols = {v["name"]: v for v in pod_spec(am)["volumes"]}
check(vols.get("alertmanager-config", {}).get("secret", {}).get("secretName") == "andai-alertmanager-config"
      and not vols.get("config"), "Alertmanager config is a Secret, not the chart's ConfigMap")
amcfg = json.loads(re.sub(r"(?m)^\s*#.*$", "", one(docs, "Secret", "andai-alertmanager-config")["stringData"]["alertmanager.yml"]))
check(amcfg["receivers"] == [{"name": "none"}] and amcfg["route"]["receiver"] == "none",
      "Alertmanager: only a null receiver by default")
check("--cluster.listen-address=" in amc["args"], "Alertmanager: no cluster listener")
check("msteams_configs" not in text, "no msteams_configs (retired Office 365 connectors)")
check(not WEBHOOK_HOSTS.search(text), "no Slack/Teams webhook URL anywhere in the render")
perses = [o for o in docs if o["kind"] in ("StatefulSet", "Deployment") and o["metadata"]["name"] == "perses"]
pvol = [v for o in perses for v in pod_spec(o)["volumes"] if v["name"] == "andai-perses-provisioning"]
dash = [i for v in pvol for i in v["configMap"]["items"] if i["path"].startswith("dashboards/")]
check(len(dash) == 6, f"Perses provisioning mounts six dashboards ({len(dash)})")
pmount = [m for o in perses for c in containers(o) for m in c.get("volumeMounts", []) if m["name"] == "andai-perses-provisioning"]
check(pmount and pmount[0]["mountPath"] == "/etc/perses/provisioning", "Perses provisioning at /etc/perses/provisioning")
pcm = one(docs, "ConfigMap", "andai-perses-provisioning")
check(pcm is not None and len([k for k in pcm["data"] if k.startswith("dashboard-")]) == 6, "provisioning ConfigMap holds six dashboards")
pconf = yaml.safe_load(one(docs, "ConfigMap", "perses")["data"]["config.yaml"])
check(pconf["datasource"]["proxy"]["allowed_hosts"] == ["prometheus"] and pconf["provisioning"]["folders"] == ["/etc/perses/provisioning"],
      "Perses: datasource proxy reaches Prometheus only; dashboards provisioned")
pcfg = yaml.safe_load(one(docs, "ConfigMap", "andai-andai-prometheus")["data"]["prometheus.yml"])
jobs = {j["job_name"] for j in pcfg["scrape_configs"]}
check({"andai-core", "andai-worker", "andai-intake", "node-exporter", "kube-state-metrics"} <= jobs and "dcgm-exporter" not in jobs,
      f"Prometheus scrapes the andai services, node-exporter and kube-state-metrics, no dcgm job ({sorted(jobs)})")
check(pcfg["rule_files"] == ["/etc/prometheus/rules/*.yml"] and pcfg["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == ["alertmanager:9093"],
      "Prometheus loads the munshi rules and sends to alertmanager:9093")
rules = one(docs, "ConfigMap", "andai-prometheus-rules")["data"]["munshi-alerts.yml"]
check(rules == (root / "andai/deploy/monitoring/prometheus/rules/munshi-alerts.yml").read_text(),
      "rules ConfigMap is the Compose rule file, byte for byte")
check(one(docs, "Service", "node-exporter") is not None and one(docs, "Service", "node-exporter")["spec"].get("clusterIP") == "None",
      "node-exporter behind a headless Service (DNS discovery as in Compose)")
check(not [o for o in docs if "pushgateway" in o["metadata"]["name"]], "no pushgateway")
check(all(s["spec"].get("type", "ClusterIP") == "ClusterIP" for s in objs(docs, "Service")
          if s["metadata"]["name"] in ("prometheus", "alertmanager", "perses")),
      "Prometheus, Alertmanager and Perses are ClusterIP only")
check(not [i for i in objs(docs, "Ingress") if i["metadata"]["name"] != "andai-portal"], "no Ingress for monitoring")
check(env_of(containers(one(docs, "Deployment", "andai-worker"))[0])["ANDAI_MONITORING"]["value"] == "1",
      "ANDAI_MONITORING=1 with monitoring on")
check(json.loads(one(docs, "ConfigMap", "andai-alerting-state")["data"]["state.json"])["receivers"] == 0,
      "receivers: 0 with monitoring on and no receiver")
no_grafana(docs, text, "monitoring")
bad = sorted(i for i in images(docs) if not catalogued(i))
check(not bad, f"monitoring: every image is catalogued ({bad or 'all'})")

# ── receivers turned on ────────────────────────────────────────────────────────────────────────
docs, text = load("receivers")
amcfg = json.loads(re.sub(r"(?m)^\s*#.*$", "", one(docs, "Secret", "andai-alertmanager-config")["stringData"]["alertmanager.yml"]))
recv = amcfg["receivers"][0]
check(recv["name"] == "munshi" and recv["slack_configs"][0]["api_url_file"] == "/etc/alertmanager/secrets/slack_webhook_url"
      and recv["msteamsv2_configs"][0]["webhook_url_file"] == "/etc/alertmanager/secrets/teams_webhook_url"
      and recv["email_configs"][0]["auth_password_file"] == "/etc/alertmanager/smtp/password",
      "receivers on: Slack, Teams (msteamsv2) and email read URLs and the password from mounted files")
check(all(c.get("text") == '{{ template "munshi.text" . }}' for k in ("slack_configs", "msteamsv2_configs", "email_configs") for c in recv[k]),
      "every receiver's text comes from munshi.tmpl")
check("msteams_configs" not in text and not WEBHOOK_HOSTS.search(text), "still no msteams_configs and no webhook URL")
check(json.loads(one(docs, "ConfigMap", "andai-alerting-state")["data"]["state.json"]) == {"receivers": 3, "names": ["email", "slack", "teams"]},
      "receivers state file counts the three receivers")

# ── dcgm-exporter (gpuMetrics) ─────────────────────────────────────────────────────────────────
docs, text = load("gpumetrics")
ds = one(docs, "DaemonSet", "dcgm-exporter")
check(ds is not None and one(docs, "Service", "dcgm-exporter") is not None, "gpuMetrics: dcgm-exporter DaemonSet and Service")
if ds:
    terms = pod_spec(ds)["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    check(terms[0]["matchExpressions"][0]["key"] == "munshi.ai/gpu-pool"
          and any(t["key"] == "nvidia.com/gpu" for t in pod_spec(ds)["tolerations"]),
          "dcgm-exporter only on GPU pool nodes, tolerating the GPU taint")
pcfg = yaml.safe_load(one(docs, "ConfigMap", "andai-andai-prometheus")["data"]["prometheus.yml"])
dj = [j for j in pcfg["scrape_configs"] if j["job_name"] == "dcgm-exporter"]
check(dj and dj[0]["dns_sd_configs"][0]["names"] == ["dcgm-exporter"], "Prometheus scrape job for the dcgm-exporter Service")
no_grafana(docs, text, "gpuMetrics")

# ── GPU pools ──────────────────────────────────────────────────────────────────────────────────
docs, text = load("gpu")
asr, llm = one(docs, "Deployment", "andai-asr"), one(docs, "Deployment", "andai-llm")
check(asr is not None and llm is not None, "ASR and LLM pool Deployments render")
pools = {}
for name, o, comp in (("asr", asr, "gpu-asr"), ("llm", llm, "gpu-llm")):
    spec = pod_spec(o)
    labels = o["spec"]["template"]["metadata"]["labels"]
    check(labels.get("app.kubernetes.io/component") == comp and labels.get("app.kubernetes.io/name") == "andai"
          and labels.get("app.kubernetes.io/instance") == "andai",
          f"{name} pool Pods labelled app.kubernetes.io/component={comp} (+ name, instance)")
    check(spec["nodeSelector"] == {"munshi.ai/gpu-pool": name}, f"{name} pool selects munshi.ai/gpu-pool={name}")
    check(any(t["key"] == "nvidia.com/gpu" and t["effect"] == "NoSchedule" for t in spec["tolerations"]),
          f"{name} pool tolerates the nvidia.com/gpu taint")
    check(int(containers(o)[0]["resources"]["limits"].get("nvidia.com/gpu", 0)) >= 1, f"{name} pool limits nvidia.com/gpu")
    pools[name] = set(spec["nodeSelector"].items())
check(not (pools["asr"] & pools["llm"]) and {k for k, _ in pools["asr"]} & {k for k, _ in pools["llm"]},
      "ASR and LLM selectors are disjoint (same key, different values)")
check(env_value(docs, "Deployment", "andai-munshi-meeting-api", "meeting-api", "TRANSCRIPTION_SERVICE_URL") == "http://andai-asr:8000",
      "bots transcribe on the ASR pool (http://andai-asr:8000)")
check(containers(asr)[0]["image"].startswith("ghcr.io/andai/munshi-transcription:"),
      "ASR pool defaults to the munshi transcription unit")

# ── bring-your-own S3 ──────────────────────────────────────────────────────────────────────────
docs, text = load("byo")
sw = [o["kind"] + "/" + o["metadata"]["name"] for o in docs if "seaweedfs" in o["metadata"]["name"]
      or o["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "seaweedfs"]
check(not sw and not [i for i in images(docs) if "seaweedfs" in i], f"BYO S3: no SeaweedFS object renders ({sw or 'none'})")
check(env_value(docs, "Deployment", "andai-munshi-meeting-api", "meeting-api", "S3_ENDPOINT") == "https://s3.example.test",
      "BYO S3: meeting-api uses the operator's endpoint")

# ── staging: the bot pool ──────────────────────────────────────────────────────────────────────
docs, text = load("staging")
sel = json.loads(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_K8S_NODE_SELECTOR"))
tol = json.loads(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_K8S_TOLERATIONS"))
check(sel == {"munshi.ai/pool": "bots"}, f"staging: bot Pods select munshi.ai/pool=bots ({sel})")
check(any(t.get("key") == "munshi.ai/pool" and t.get("value") == "bots" and t.get("effect") == "NoSchedule" for t in tol),
      "staging: bot Pods tolerate munshi.ai/pool=bots:NoSchedule")
check(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_K8S_SHM_SIZE") == "2Gi"
      and env_value(docs, "Deployment", rt, "runtime", "RUNTIME_BOT_MEMORY_MB") == "2560"
      and env_value(docs, "Deployment", rt, "runtime", "RUNTIME_MAX_ACTIVE_BOTS") == "3",
      "staging: 2Gi shm, 2560 MiB bots, cap 3 from values")
rtpod = pod_spec(one(docs, "Deployment", rt))
check("munshi.ai/pool" not in (rtpod.get("nodeSelector") or {}), "staging: the runtime Deployment itself stays off the bot pool")
check(not objs(docs, "Deployment", "andai-asr") and not objs(docs, "Deployment", "andai-llm"), "staging: GPU pools off")
check(any(i.startswith("prom/prometheus:") for i in images(docs)) and one(docs, "StatefulSet", "seaweedfs-master") is not None,
      "staging: SeaweedFS and monitoring on")
bad = sorted(i for i in images(docs) if not catalogued(i))
check(not bad, f"staging: every image is catalogued ({bad or 'all'})")
docs, text = load("staginggpu")
bots = set(json.loads(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_K8S_NODE_SELECTOR")).items())
gpu = set(pod_spec(one(docs, "Deployment", "andai-asr"))["nodeSelector"].items()) | set(pod_spec(one(docs, "Deployment", "andai-llm"))["nodeSelector"].items())
check(not (bots & gpu), "the bot pool shares no label with either GPU pool")

# ── load test overlay ──────────────────────────────────────────────────────────────────────────
docs, text = load("loadtest")
check(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_BOT_MEMORY_MB") == "256", "loadtest: bot memory 256 MiB")
check(env_value(docs, "Deployment", rt, "runtime", "BROWSER_IMAGE").startswith("ghcr.io/andai/munshi-mock-bot:"), "loadtest: the mock bot image")
check(env_value(docs, "Deployment", rt, "runtime", "RUNTIME_MAX_ACTIVE_BOTS") == "37", "loadtest: cap set at install (37)")

sys.exit(1 if failures else 0)
PY

[ "$fail" -eq 0 ] && { echo "andai chart template: PASS"; exit 0; } || { echo "andai chart template: FAIL"; exit 1; }
