# deploy/helm/charts/andai/files

Files the andai chart's templates read with `.Files.Get`. `monitoring/` holds the Compose monitoring
configs (rules, notification templates, dashboards), copied byte for byte and checked by
`deploy/helm/tests/test_andai_monitoring_sync.sh`.
