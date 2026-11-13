# helm · tests

Smoke tests for the `munshi` Helm chart: `test_helm_lint.sh` (chart lint) and `test_template.sh` (render/template validation, including the `storage.s3` wiring and the renders it must refuse). Run as part of the helm deploy checks.

The `andai` umbrella chart's static tests (`make -C deploy/helm andai-test`):

- `test_andai_monitoring_sync.sh` — `charts/andai/files/monitoring` is byte-for-byte `andai/deploy/monitoring` (`--write` copies it).
- `test_andai_lint.sh` — vendors the dependencies, checks the pinned versions, the KEK refusal with the defaults, and that values-test, -staging and -loadtest lint and render clean.
- `test_andai_template.sh` — renders each configuration (values-test, monitoring and receivers on, GPU pools, dcgm-exporter, bring-your-own S3 and Postgres, staging, load test) and asserts the andai Deployments, migrations hook, KEK mount, SeaweedFS SSE-S3 and telemetry off, disjoint GPU pools, the bot pool and sizing, Alertmanager receivers and Secrets, Perses dashboards, no Grafana and catalogued images only, plus every render-time refusal.

Install-leg helpers (the `validate-helm-k3s` release leg uses both):

- `s3-fixture.yaml` — a TEST-ONLY S3 (versitygw, POSIX backend, pinned by digest) plus the Job that creates its `munshi-recordings` bucket. The chart runs no object store, so an install from `values-test.yaml` needs this running first; its header has the commands, including the random-credential Secret it reads.
- `recording_roundtrip.py` — run inside the meeting-api pod: uploads a recording the way a bot does, finalizes and plays it back through the gateway, deletes it, and lists the bucket prefix at each step.
