# andai/deploy/monitoring/alertmanager/templates

`munshi.tmpl` defines `munshi.title` and `munshi.text`, the only text any receiver sends: alert name,
service, severity, the rule's fixed summary, status and counts (D-62). Mounted at
`/etc/alertmanager/templates`; `services/worker/tests/test_alerting_config.py` checks what it may reference.
