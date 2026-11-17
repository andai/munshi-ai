# helm · tests · staging

TEST-ONLY fixtures for the staging witness of the `andai` chart: one k3s node in a Linux VM (Lima on
a Mac), its local-path storage on a LUKS volume, synthetic users and the mock bot only. Nothing here
ships in a release or belongs in a production install; the install guide is
`andai/docs/kubernetes.md`.

| File | What it is |
|---|---|
| `values-staging-local.yaml` | Values on top of `charts/andai/values-staging.yaml`: images imported (pull policy `Never`), portal origin on the operator's port-forward, mail to Mailpit, minutes from the fake model route, monitoring on with the email, Slack and Teams receivers pointed at the fixtures below. No secret, no real endpoint. |
| `mailpit.yaml` | Mailpit (MIT): catches every mail (SMTP `mailpit:1025`), relays nothing; UI/API on `mailpit:8025`. |
| `alert-sink.yaml` | The andai-core image's `andai_core.testing.alert_sink`: stands in for the Slack and Teams webhooks and scans what it caught for PII. |
| `fake-llm.yaml` | The andai-core image's deterministic `andai_core.testing.fake_llm`, so mock meetings get minutes without a model and the minutes stage does not stay owed. |
| `traefik-config.yaml` | A k3s `HelmChartConfig` that turns off the bundled Traefik's version check and usage reporting (copied into `/var/lib/rancher/k3s/server/manifests/`). |
| `egress-check.sh` | Captures the VM's outbound traffic (headers only) and the CoreDNS query log, and reports destinations by class; exits 1 on a destination outside the allowed classes or a lookup of SeaweedFS's telemetry host. |
| `allow-hosts.txt` | The names an approved download window may reach (registries, package mirrors), read by `egress-check.sh report`. |

## Order

1. VM with a LUKS data volume mounted at `/var/lib/munshi-crypt`; k3s with
   `--default-local-storage-path=/var/lib/munshi-crypt/k3s-storage` and `--secrets-encryption`;
   `traefik-config.yaml` in the k3s manifests directory; `egress-check.sh start`.
2. Images imported into the node's containerd (`docker save … | k3s ctr images import -`); every
   image the render names must be present, since the pull policy is `Never`.
3. Namespace `munshi` and its Secrets as in `andai/docs/kubernetes.md` (generated on the spot,
   never written to the repo), plus `andai-alerting` with the sink URLs (header of `alert-sink.yaml`).
4. `kubectl -n munshi apply -f mailpit.yaml`; `sed "s/STAGING_TAG/<tag>/" alert-sink.yaml` and
   `fake-llm.yaml` piped to `kubectl -n munshi apply -f -`.
5. Label the node for the bot pool only (`kubectl label node <node> munshi.ai/pool=bots`; no taint
   on a single node), then the `helm upgrade --install` in the header of `values-staging-local.yaml`.
6. Storage alert: mount a small tmpfs in the VM at `/host/munshi-fill` (node-exporter reports it
   under the rule's `^/host(/.*)?$`) and fill it past 80 %; never fill the LUKS volume or the root.
7. Load test: add `-f deploy/helm/charts/andai/values-loadtest.yaml --set munshi.runtime.k8sShmSize=`
   and size `munshi.runtime.workloadResources.meetingBot.memoryMb` from a measured mock bot (an
   amd64 mock under Rosetta needs more than the overlay's 256 Mi); reach andai-core with
   `kubectl port-forward svc/andai-core 18300:8000` for the driver.
8. `egress-check.sh report` at the end: 0 disallowed destinations, 0 telemetry lookups. The mock
   bot starts through `npx`, which reaches registry.npmjs.org; expect those flows until the mock
   image starts without npm.

Tear the fixtures down with the namespace (`kubectl delete namespace munshi`).
