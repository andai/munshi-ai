# deploy/helm/charts/andai/files/monitoring

Byte-for-byte copies of the Compose monitoring configs in `andai/deploy/monitoring` (flat, because a
ConfigMap key cannot hold a directory), read by `templates/monitoring-config.yaml` with `.Files.Get`.
Edit the sources, then run `make -C deploy/helm andai-sync-monitoring`;
`deploy/helm/tests/test_andai_monitoring_sync.sh` fails on any drift.

| Copy | Source (`andai/deploy/monitoring/`) |
|---|---|
| `prometheus.yml` | `prometheus/prometheus.yml` (the chart adds the kube-state-metrics job and, with `gpuMetrics.enabled`, the dcgm-exporter job) |
| `munshi-alerts.yml` | `prometheus/rules/munshi-alerts.yml` |
| `munshi.tmpl` | `alertmanager/templates/munshi.tmpl` |
| `perses-project.yaml`, `perses-datasource.yaml` | `perses/provisioning/project.yaml`, `datasource.yaml` |
| `dashboard-*.yaml` | `perses/provisioning/dashboards/*.yaml` |
