# andai/deploy/monitoring/perses/provisioning

Resources Perses applies at start-up and every hour: `project.yaml` (project `munshi`),
`datasource.yaml` (the stack's Prometheus, default) and `dashboards/`. Perses reads only `.yaml`,
`.yml` and `.json` files here.
