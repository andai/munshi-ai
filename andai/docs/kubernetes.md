# munshi.ai on Kubernetes

One Helm release runs the whole product: the `andai` umbrella chart in
`deploy/helm/charts/andai`. It brings in the munshi platform chart (`deploy/helm/charts/munshi`),
the andai services (andai-core, andai-worker, andai-intake and the portal), object storage
(SeaweedFS with server-side encryption, or your own S3 bucket), optional GPU pools for
speech-to-text and the language model, and opt-in monitoring (Prometheus, Alertmanager and Perses;
no Grafana). Every setting is a value, and `values.yaml` documents each one.

What is proved: the chart lints and renders as `deploy/helm/tests` asserts (`make -C deploy/helm
andai-test`). Installs on a cluster are proved separately (staging witness, scale checkpoint).

## Prerequisites

- A Kubernetes cluster (k3s works; one node is enough for staging) and Helm 3.
- A default StorageClass, or name one in the `*.storageClass` / `persistence` values. Its volumes
  must be encrypted: per-volume LUKS, encrypted cloud volumes, or the node's local storage path on
  dm-crypt (k3s `local-path` writes plain host directories). See `encryption-at-rest.md`.
- Secrets encrypted in the cluster datastore: start k3s with `--secrets-encryption` (off by
  default), or use your platform's equivalent.
- An ingress controller for the portal (k3s ships Traefik) and DNS for its host name.
- An SMTP relay you run or trust for outgoing mail.
- The images `ghcr.io/andai/munshi-*:<VERSION>` (`global.imageTag`, the chart's appVersion) in a
  registry the nodes can pull from, or imported into the nodes' containerd.
- The chart's dependencies: `make -C deploy/helm andai-deps` runs `helm dependency build`, which
  fetches seaweedfs 4.53.0, prometheus-community/prometheus 29.45.1 and perses 0.24.0 as pinned in
  `Chart.lock` (network needed once).

## 1. Create the namespace and its Secrets

The chart never holds a secret in its values; it references Secrets by name. Create them first
(names are the defaults in `values.yaml`). Every value below is generated on the spot; never paste
real values into a values file or a shell history you keep.

```bash
NS=munshi
kubectl create namespace $NS

# The key-encryption key: `active` (base64 of 32 bytes) and `retired` (empty unless rotating).
kubectl -n $NS create secret generic andai-kek \
  --from-literal=active="$(openssl rand -base64 32)" --from-literal=retired=

# The andai database roles (URL-safe values).
kubectl -n $NS create secret generic andai-db \
  --from-literal=owner-password="$(openssl rand -hex 24)" \
  --from-literal=app-password="$(openssl rand -hex 24)"

# The munshi platform secrets (deploy/helm/charts/munshi/values.yaml lists every key).
kubectl -n $NS create secret generic munshi-secrets \
  --from-literal=ADMIN_API_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=INTERNAL_API_SECRET="$(openssl rand -hex 32)" \
  --from-literal=MUNSHI_SYSTEM_WEBHOOK_SECRET="$(openssl rand -hex 32)" \
  --from-literal=MUNSHI_DISPATCH_SIGNING_KEY="$(openssl rand -hex 32)" \
  --from-literal=NEXTAUTH_SECRET="$(openssl rand -hex 32)" \
  --from-literal=TRANSCRIPTION_SERVICE_TOKEN="<the token of your speech-to-text endpoint>"

# The SMTP password, if the relay needs one.
kubectl -n $NS create secret generic andai-smtp --from-literal=password="<relay password>"
```

The bundled Postgres superuser password is a munshi chart value (`munshi.database.password`; the
munshi chart writes it into its own `postgres-credentials` Secret). Pass it from a file that is not
tracked (`--set-file munshi.database.password=<file>` or `-f <local values>`); the chart refuses
an empty value and the old default `postgres`.

### Object storage

**SeaweedFS (default).** The chart runs SeaweedFS (master, volume, filer with the S3 gateway) and
creates the recordings bucket with default encryption SSE-S3 (AES256). It needs two more Secrets:

```bash
AK="$(openssl rand -hex 10)"; SK="$(openssl rand -hex 20)"
# The S3 identity meeting-api (and the bucket Job) use: Admin to create the bucket and set its
# encryption, then read and write recordings.
kubectl -n $NS create secret generic andai-seaweedfs-s3 --from-literal=seaweedfs_s3_config="$(printf \
  '{"identities":[{"name":"munshi","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}' "$AK" "$SK")"
kubectl -n $NS create secret generic andai-s3 \
  --from-literal=AWS_ACCESS_KEY_ID="$AK" --from-literal=AWS_SECRET_ACCESS_KEY="$SK"
unset AK SK
# The SSE-S3 key-encryption key: 64 hex characters.
kubectl -n $NS create secret generic andai-seaweedfs-sse --from-literal=kek="$(openssl rand -hex 32)"
```

Every SeaweedFS master runs with `-telemetry=false` (SeaweedFS reports usage to its vendor by
default); the chart refuses a render without it. SeaweedFS documents no rotation for the SSE-S3
key: keep it for the life of the data and back it up apart from the data.

**Your own bucket.** Set `seaweedfs.enabled=false` and point `munshi.storage.s3` at it
(`endpoint`, `bucket`, `existingSecret` with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`). The
bucket must exist and its own default encryption applies; the chart creates nothing.

## 2. Install

Write the deployment's own values in a file you keep outside the repository, for example:

```yaml
andai:
  kek:
    existingSecret: andai-kek
  portal:
    origin: https://munshi.example.com
    ingress:
      enabled: true
      className: traefik
      host: munshi.example.com
      tls: [{ hosts: [munshi.example.com], secretName: munshi-tls }]
  smtp:
    host: smtp.example.com
    user: munshi
    existingSecret: andai-smtp
  intake:
    addresses: notetaker@example.com
munshi:
  meetingApi:
    transcriptionServiceUrl: http://stt.example.internal:8083   # or the ASR pool, below
  runtime:
    maxActiveBots: 22                                            # see "Sizing the bot cap"
```

```bash
make -C deploy/helm andai-deps
helm upgrade --install andai deploy/helm/charts/andai -n $NS \
  -f my-values.yaml --set-file munshi.database.password=./pg-password --wait --timeout 15m
```

What happens:

1. The platform, SeaweedFS and the andai services start. Their readiness does not need the andai
   schema.
2. `andai-migrations` (a hook Job) creates the `andai` database and its roles (`db-init.sql`, psql
   as the Postgres superuser), then runs `alembic upgrade head` as `andai_owner`. With the bundled
   Postgres it runs after install; with an external Postgres whose admin Secret already exists it
   runs before install. It runs before every upgrade.
3. `andai-seaweedfs-bucket` creates the bucket if it is missing and sets SSE-S3 as its default
   encryption, then reads it back.

Then create the first org and its owner (a set-password link is mailed):

```bash
kubectl -n $NS exec deploy/andai-core -- python -m andai_core bootstrap --email owner@example.com --org "Example Ltd"
```

The andai workloads have fixed names (`deploy/andai-core`, `andai-worker`, `andai-intake`,
`andai-portal`), as the runbooks use them; install one release per namespace. Only the portal is
exposed (its Ingress). andai-core, the platform, SeaweedFS and monitoring stay ClusterIP. The
notetaker mailbox's SMTP port has its own Service, `andai-intake-smtp`; pick its type
(`andai.intake.smtpService.type`: ClusterIP behind your MX relay, NodePort or LoadBalancer).

Upgrades are `helm upgrade` with the same values; migrations run first.

## 3. Node pools

### Bot pool

Meeting bots are Pods the platform's runtime spawns; `munshi.runtime.nodeSelector` and
`munshi.runtime.tolerations` place them (they reach the runtime as `RUNTIME_K8S_NODE_SELECTOR` /
`RUNTIME_K8S_TOLERATIONS`, and apply to agent worker Pods too). The runtime Deployment itself and
everything else keep `global.nodeSelector` / `global.tolerations`.

On a multi-node cluster, label and taint the bot nodes and set both values:

```bash
kubectl label node <node> munshi.ai/pool=bots
kubectl taint node <node> munshi.ai/pool=bots:NoSchedule
```

```yaml
munshi:
  runtime:
    nodeSelector: { munshi.ai/pool: bots }
    tolerations: [{ key: munshi.ai/pool, operator: Equal, value: bots, effect: NoSchedule }]
```

On a single node, only label it: a NoSchedule taint on the only node would leave every other Pod
Pending (`values-staging.yaml` does this). The bot pool must not share a label with a GPU pool; the
chart refuses it.

Each bot Pod gets a 2 GiB memory-backed `/dev/shm` (`munshi.runtime.k8sShmSize`) and 1 CPU and
2560 MiB as request and limit (`munshi.runtime.workloadResources.meetingBot`). Shared memory counts
against the memory limit, so do not lower `memoryMb` below 2560 while shm is 2Gi.

### Sizing the bot cap

`munshi.runtime.maxActiveBots` caps the bots starting or running at once across the cluster: a
dispatch over it fails at once as `capacity_exhausted` instead of leaving a bot Pod Pending. Size
it to the bot nodes:

    cap = floor(sum of the bot nodes' allocatable memory in MiB / 2560) - headroom

For example two bot nodes with about 30 GiB allocatable each: floor(61440 / 2560) = 24; with a
headroom of 2, a cap of 22. Keep `munshi.runtime.replicaCount: 1`: the cap is counted by one
runtime process. Measure what a node really holds with the load test (`load-test.md`,
`values-loadtest.yaml`).

### GPU pools

Two separate pools, each on nodes labelled `munshi.ai/gpu-pool`:

| Pool | Value | Runs | Node label |
|---|---|---|---|
| Speech-to-text | `gpuPools.asr` | the munshi transcription unit (`ghcr.io/andai/munshi-transcription`), Service `andai-asr:8000` | `munshi.ai/gpu-pool=asr` |
| Language model | `gpuPools.llm` | an OpenAI-compatible server you name (`gpuPools.llm.image` is required), Service `andai-llm` | `munshi.ai/gpu-pool=llm` |

Each Pod requests whole GPUs (`nvidia.com/gpu`, `gpus` per replica) and tolerates the
`nvidia.com/gpu` NoSchedule taint. The GPU nodes need the NVIDIA driver and the device plugin (or
the NVIDIA GPU Operator, which installs both); taint them so nothing else lands there:

```bash
kubectl label node <gpu-node> munshi.ai/gpu-pool=asr
kubectl taint node <gpu-node> nvidia.com/gpu=present:NoSchedule
```

With the ASR pool on, set `munshi.meetingApi.transcriptionServiceUrl: http://andai-asr:8000` (the
chart refuses anything else) so bots transcribe on it; the unit checks the platform's
`TRANSCRIPTION_SERVICE_TOKEN`. With the LLM pool on, set `andai.llm.baseUrl: http://andai-llm:<port>`
and `andai.llm.model`. The ASR unit downloads its model weights at start into an emptyDir.

## 4. Monitoring (opt-in)

`monitoring.enabled=true` adds Prometheus 3.13 LTS (with Alertmanager, node-exporter and
kube-state-metrics) and Perses, with the rules, notification text and six dashboards of the
Compose profile (`monitoring.md`). Nothing is exposed; reach the UIs with port-forward:

```bash
kubectl -n $NS port-forward svc/perses 8080:8080
kubectl -n $NS port-forward svc/prometheus 9090:9090
kubectl -n $NS port-forward svc/alertmanager 9093:9093
```

Every receiver is off by default (Alertmanager routes to a receiver with no integrations, and
andai-worker keeps sending its own alert mail). Turn receivers on with values; webhook URLs stay in
a Secret mounted into Alertmanager and are referenced as files:

```bash
kubectl -n $NS create secret generic andai-alerting \
  --from-file=slack_webhook_url=./slack-url --from-file=teams_webhook_url=./teams-url
```

```yaml
monitoring:
  enabled: true
  alerting:
    existingSecret: andai-alerting
    email: { enabled: true }      # to andai.alertEmail, through andai.smtp
    slack: { enabled: true }
    teams: { enabled: true }      # a Teams Workflows webhook (msteamsv2)
```

With at least one receiver on, andai-worker's own alert mail stands down (the state file
`andai-alerting-state`). The storage alert watches the node's root filesystem as node-exporter
reports it (`^/$`); edit the rule's mount regex if your volumes live elsewhere.

GPU metrics (`gpuMetrics.enabled`) run NVIDIA dcgm-exporter on the GPU pool nodes. Its image is
under NVIDIA's own terms and is not cleared: the chart names none and refuses to render it until
`gpuMetrics.image` is set to an image you have cleared.

## 5. Key rotation

Follow `runbooks/key-rotation.md` with the Secret instead of the env file: put the new key in
`active` and the old one in `retired`, roll the services (`kubectl rollout restart deploy/andai-core
deploy/andai-worker`), then either `kubectl exec deploy/andai-core -- python -m andai_core
kek-rewrap` or one `helm upgrade` with `andai.kekRewrap.run=true` (a Job after the services roll),
and back to `false`. `kek-status` must show no value on the old key before it leaves `retired`.

## 6. What the chart does not do

- It does not encrypt volumes, back up Postgres or SeaweedFS, or rotate the SeaweedFS SSE-S3 key.
- It does not install the NVIDIA driver, device plugin or GPU Operator.
- It does not configure the munshi platform's agent features (agent-api's model route and the
  hardened agent worker image the Compose overlay sets); leave them unset unless you run an
  in-house model route for them.
