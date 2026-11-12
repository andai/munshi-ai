"""The alert rules and the Prometheus config keep the notification surface closed (OPS-05, D-62).

YAML only, no Docker: every rule has a `service` and a `severity` from closed sets and one fixed
`summary` with no template; no rule aggregates, matches or relabels by a label outside the metric
label allowlist (`andai_core.metrics.LABEL_ALLOWLIST`, plus `job`/`instance` matchers on `up`);
every metric a rule reads is in the catalogue; every alert has promtool unit tests that see it both
quiet and firing; the runbook covers every alert. The thresholds themselves are proved by
`promtool test rules` (`make -C andai monitoring-check`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from andai_core.metrics import LABEL_ALLOWLIST

ANDAI = Path(__file__).resolve().parents[3]
MONITORING = ANDAI / "deploy" / "monitoring"
PROMETHEUS = MONITORING / "prometheus"
RULES = PROMETHEUS / "rules" / "munshi-alerts.yml"
RULE_TESTS = PROMETHEUS / "rules" / "tests" / "munshi-alerts.test.yml"
METRICS_DOC = ANDAI / "docs" / "metrics.md"
RUNBOOK = ANDAI / "docs" / "runbooks" / "alerts.md"

SERVICES = {
    "bot-joins",
    "pipeline",
    "storage",
    "erasure",
    "andai-core",
    "andai-worker",
    "andai-intake",
    "node-exporter",
}
SEVERITIES = {"critical", "warning"}
EXPECTED_ALERTS = {
    "MunshiCanaryFailing",
    "MunshiJoinSuccessLow",
    "MunshiPipelineBacklog",
    "MunshiStorageHigh",
    "MunshiErasureSlaAtRisk",
    "MunshiTargetDown",
}
SCRAPE_JOBS = {"andai-core", "andai-worker", "andai-intake", "node-exporter"}
# Metrics that do not come from munshi's own services.
EXTERNAL_METRICS = {"up", "node_filesystem_avail_bytes", "node_filesystem_size_bytes"}
# `job` is matched only on `up` (target health); it is Prometheus' own label, not a service's.
TARGET_LABELS = {"job"}

GROUPING = re.compile(r"\b(by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)")
SELECTOR = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{([^}]*)\}")
MATCHER_LABEL = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)")
FUNCTIONS = {
    "max",
    "min",
    "sum",
    "avg",
    "count",
    "absent",
    "rate",
    "increase",
    "by",
    "without",
    "on",
    "ignoring",
    "and",
    "or",
    "unless",
    "group_left",
    "group_right",
}
IDENTIFIER = re.compile(r"\b[a-zA-Z_:][a-zA-Z0-9_:]*\b")


def _rules() -> list[dict]:
    doc = yaml.safe_load(RULES.read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def _strip_selectors(expr: str) -> str:
    return re.sub(r"\{[^}]*\}", "", expr)


def _metric_names(expr: str) -> set[str]:
    bare = _strip_selectors(expr)
    bare = GROUPING.sub(" ", bare)
    names = set()
    for token in IDENTIFIER.findall(bare):
        if token in FUNCTIONS or re.fullmatch(r"\d+", token):
            continue
        names.add(token)
    return names


def _catalogue() -> set[str]:
    return set(re.findall(r"`(munshi_[a-z0-9_]+)`", METRICS_DOC.read_text()))


def test_the_rule_file_has_the_planned_alerts() -> None:
    names = {rule["alert"] for rule in _rules()}
    assert names == EXPECTED_ALERTS


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: f"{r['alert']}-{r['labels']['service']}")
def test_every_rule_has_closed_labels_and_a_fixed_summary(rule: dict) -> None:
    assert set(rule) <= {"alert", "expr", "for", "labels", "annotations"}
    assert set(rule["labels"]) == {"service", "severity"}
    assert rule["labels"]["service"] in SERVICES
    assert rule["labels"]["severity"] in SEVERITIES
    assert set(rule["annotations"]) == {"summary"}
    summary = rule["annotations"]["summary"]
    assert isinstance(summary, str) and summary.strip()
    assert "{{" not in summary and "$" not in summary
    assert not re.search(r"https?://|@", summary)


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: f"{r['alert']}-{r['labels']['service']}")
def test_rules_group_and_match_only_by_allowlisted_labels(rule: dict) -> None:
    expr = rule["expr"]
    assert "label_replace" not in expr and "label_join" not in expr
    for _, labels in GROUPING.findall(expr):
        for label in filter(None, (part.strip() for part in labels.split(","))):
            assert label in LABEL_ALLOWLIST, f"{rule['alert']} groups by {label}"
    for metric, matchers in SELECTOR.findall(expr):
        for label in MATCHER_LABEL.findall(matchers):
            if metric == "up":
                assert label in TARGET_LABELS, f"{rule['alert']} matches up on {label}"
            elif metric.startswith("node_filesystem_"):
                assert label == "mountpoint", f"{rule['alert']} matches {metric} on {label}"
            else:
                assert label in LABEL_ALLOWLIST, f"{rule['alert']} matches {metric} on {label}"


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: f"{r['alert']}-{r['labels']['service']}")
def test_rules_read_only_catalogue_metrics(rule: dict) -> None:
    allowed = _catalogue() | EXTERNAL_METRICS
    unknown = _metric_names(rule["expr"]) - allowed
    assert not unknown, f"{rule['alert']} reads {sorted(unknown)}"


def test_the_target_down_rules_cover_every_scrape_job_once() -> None:
    covered = [r["labels"]["service"] for r in _rules() if r["alert"] == "MunshiTargetDown"]
    assert sorted(covered) == sorted(SCRAPE_JOBS)


def test_prometheus_scrapes_the_planned_jobs_without_relabelling() -> None:
    config = yaml.safe_load((PROMETHEUS / "prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert set(jobs) == SCRAPE_JOBS
    for job in jobs.values():
        assert "relabel_configs" not in job and "metric_relabel_configs" not in job
        assert job.get("metrics_path", "/metrics") == "/metrics"
    for name in ("andai-core", "andai-worker", "andai-intake"):
        assert jobs[name]["static_configs"] == [{"targets": [f"{name}:8000"]}]
    assert jobs["node-exporter"]["dns_sd_configs"][0]["names"] == ["node-exporter"]
    assert config["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    targets = config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"]
    assert "remote_write" not in config and "remote_read" not in config


def test_every_alert_is_unit_tested_quiet_and_firing() -> None:
    doc = yaml.safe_load(RULE_TESTS.read_text())
    assert doc["rule_files"] == ["../munshi-alerts.yml"]
    quiet: set[str] = set()
    firing: set[str] = set()
    for case in doc["tests"]:
        for check in case["alert_rule_test"]:
            (firing if check["exp_alerts"] else quiet).add(check["alertname"])
    assert quiet == EXPECTED_ALERTS
    assert firing == EXPECTED_ALERTS


def test_unit_tests_expect_the_rule_labels_and_summaries() -> None:
    by_name: dict[str, list[dict]] = {}
    for rule in _rules():
        by_name.setdefault(rule["alert"], []).append(rule)
    doc = yaml.safe_load(RULE_TESTS.read_text())
    for case in doc["tests"]:
        for check in case["alert_rule_test"]:
            for alert in check["exp_alerts"]:
                rules = by_name[check["alertname"]]
                labels = alert["exp_labels"]
                assert any(
                    labels["service"] == r["labels"]["service"]
                    and labels["severity"] == r["labels"]["severity"]
                    and alert["exp_annotations"] == r["annotations"]
                    for r in rules
                ), check["alertname"]


def test_the_runbook_covers_every_alert() -> None:
    text = RUNBOOK.read_text()
    for name in EXPECTED_ALERTS:
        assert f"## {name}" in text, name
