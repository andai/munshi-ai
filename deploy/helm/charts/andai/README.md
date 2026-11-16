# deploy/helm/charts/andai — the munshi.ai umbrella chart

One Helm release for the whole product: the munshi platform (dependency `munshi`,
`file://../munshi`, never copied), the andai services (andai-core, andai-worker, andai-intake, the
portal), object storage (SeaweedFS 4.53 with SSE-S3 and telemetry off, or your own S3 bucket), GPU
pools for speech-to-text and the language model, the bot pool and bot sizing, and opt-in monitoring
(Prometheus 3.13 LTS, Alertmanager, node-exporter, kube-state-metrics and Perses; no Grafana).

Install guide: [`andai/docs/kubernetes.md`](../../../../andai/docs/kubernetes.md). Every value is
documented in [`values.yaml`](values.yaml).

| File | What it is |
|---|---|
| `Chart.yaml`, `Chart.lock` | The four dependencies, pinned; `make -C deploy/helm andai-deps` vendors them into `charts/` (git-ignored archives) |
| `values.yaml` | Defaults. Secrets are only ever Secret names; with the defaults the render stops on the missing KEK Secret |
| `values-test.yaml` | The render fixture of `deploy/helm/tests/test_andai_*.sh` |
| `values-staging.yaml` | One-node staging (k3s in a VM): bot pool label, SeaweedFS and monitoring on, GPU pools off |
| `values-loadtest.yaml` | On top of staging: the mock bot at 256 MiB; the cap is set at install |
| `templates/` | The andai workloads, Jobs, GPU pools, monitoring config and render-time guards |
| `files/monitoring/` | Copies of `andai/deploy/monitoring` (rules, notification templates, dashboards) |

Checks (no cluster): `make -C deploy/helm andai-test` — monitoring files in sync, lint, render
assertions and refusals; `node scripts/gates.mjs helm` runs it with the munshi chart's tests.

Values a subchart reads cannot be computed by this chart. Where one must agree with an andai value
(the S3 endpoint, the ASR URL, the Secrets mounted into Alertmanager), both are set in
`values.yaml` and `templates/validate.yaml` refuses a render where they disagree.
