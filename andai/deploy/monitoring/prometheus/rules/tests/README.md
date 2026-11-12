# andai/deploy/monitoring/prometheus/rules/tests

`munshi-alerts.test.yml`: `promtool test rules` cases proving each alert fires at its threshold and
stays quiet just below it. Run by `make -C andai monitoring-check` in the pinned Prometheus image.
