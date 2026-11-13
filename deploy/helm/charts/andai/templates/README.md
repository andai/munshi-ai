# andai chart · templates

Kubernetes manifests rendered by the `andai` umbrella chart, next to those of its subcharts (munshi,
seaweedfs, prometheus, perses). `_helpers.tpl` holds the `andai.`-prefixed helpers; `validate.yaml`
renders nothing and refuses a render with a missing Secret name or values that disagree with a
subchart's (S3 endpoint, transcription URL, Alertmanager Secret mounts).

| File | What it renders |
|---|---|
| `configmap-andai.yaml` | Non-secret settings of andai-core, andai-worker and andai-intake; the alert receivers state file |
| `deployment-andai-{core,worker,intake}.yaml`, `deployment-portal.yaml` | The four andai Deployments (fixed names) |
| `services.yaml`, `ingress-portal.yaml` | Their Services (SMTP on its own Service) and the portal's Ingress, the only one |
| `job-andai-migrations.yaml` | db-init and Alembic as a hook Job |
| `job-kek-rewrap.yaml` | `kek-rewrap` after a key rotation (`andai.kekRewrap.run`) |
| `job-seaweedfs-bucket.yaml` | The recordings bucket on SeaweedFS with default SSE-S3 |
| `gpu-asr.yaml`, `gpu-llm.yaml` | The speech-to-text and language-model GPU pools |
| `monitoring-config.yaml` | Prometheus config and rules, the Alertmanager config Secret, Perses provisioning |
| `dcgm-exporter.yaml` | NVIDIA dcgm-exporter (opt-in `gpuMetrics`) |
