"""The Perses dashboards as code cover SC5 and read only catalogue metrics (OPS-05, D-62).

YAML only, no Docker: six dashboards exist (joins, ASR, GPU, pipeline, intake, email) in project
`munshi`; every panel query names only metrics from `andai/docs/metrics.md` (or dcgm-exporter 4.9's
GPU series); the GPU dashboard says it shows no data without GPU nodes and falls back to 0; no
dashboard variable or query grouping is an org, user, meeting or any label outside the allowlist;
every panel is laid out once; the datasource is the stack's Prometheus. Linting against the pinned
Perses image is `make -C andai monitoring-check`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from andai_core.metrics import LABEL_ALLOWLIST

ANDAI = Path(__file__).resolve().parents[3]
PERSES = ANDAI / "deploy" / "monitoring" / "perses"
PROVISIONING = PERSES / "provisioning"
DASHBOARDS = PROVISIONING / "dashboards"
METRICS_DOC = ANDAI / "docs" / "metrics.md"

EXPECTED = {"joins", "asr", "gpu", "pipeline", "intake", "email"}
# dcgm-exporter 4.9 default counters (etc/default-counters.csv at 4.9.0).
DCGM = {
    "DCGM_FI_DEV_GPU_UTIL_RATIO",
    "DCGM_FI_DEV_MEMORY_UTIL_RATIO",
    "DCGM_FI_DEV_FB_USED_BYTES",
    "DCGM_FI_DEV_FB_CAPACITY_BYTES",
}
FUNCTIONS = {
    "sum", "max", "min", "avg", "count", "by", "without", "on", "ignoring", "and", "or",
    "unless", "rate", "increase", "histogram_quantile", "time", "vector", "clamp_min",
}
GROUPING = re.compile(r"\b(by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)")
SELECTOR = re.compile(r"\{([^}]*)\}")
MATCHER_LABEL = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)")
RANGE = re.compile(r"\[[0-9smhdw]+\]")
IDENTIFIER = re.compile(r"\b[a-zA-Z_:][a-zA-Z0-9_:]*\b")
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
IDENTITY_WORDS = {"org", "org_id", "user", "user_id", "meeting", "meeting_id", "team", "email"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _dashboards() -> dict[str, dict]:
    return {path.stem: _load(path) for path in sorted(DASHBOARDS.glob("*.yaml"))}


def _queries(dashboard: dict) -> list[str]:
    return [
        query["spec"]["plugin"]["spec"]["query"]
        for panel in dashboard["spec"]["panels"].values()
        for query in panel["spec"].get("queries", [])
    ]


def _catalogue() -> set[str]:
    return set(re.findall(r"`(munshi_[a-z0-9_]+)`", METRICS_DOC.read_text()))


def _metrics(expr: str) -> set[str]:
    bare = RANGE.sub(" ", SELECTOR.sub(" ", expr))
    bare = GROUPING.sub(" ", bare)
    names = set()
    for token in IDENTIFIER.findall(bare):
        if token in FUNCTIONS or re.fullmatch(r"\d+", token):
            continue
        for suffix in HISTOGRAM_SUFFIXES:
            if token.endswith(suffix) and token[: -len(suffix)] in _catalogue():
                token = token[: -len(suffix)]
        names.add(token)
    return names


def test_the_six_dashboards_exist_in_the_munshi_project() -> None:
    dashboards = _dashboards()
    assert set(dashboards) == EXPECTED
    for name, dashboard in dashboards.items():
        assert dashboard["kind"] == "Dashboard"
        assert dashboard["metadata"] == {"name": name, "project": "munshi"}
        assert dashboard["spec"]["panels"], name
    project = _load(PROVISIONING / "project.yaml")
    assert project["kind"] == "Project" and project["metadata"]["name"] == "munshi"


def test_the_datasource_is_the_stack_prometheus_by_default() -> None:
    datasource = _load(PROVISIONING / "datasource.yaml")
    assert datasource["kind"] == "GlobalDatasource"
    assert datasource["spec"]["default"] is True
    plugin = datasource["spec"]["plugin"]
    assert plugin["kind"] == "PrometheusDatasource"
    assert plugin["spec"]["proxy"]["spec"]["url"] == "http://prometheus:9090"
    config = _load(PERSES / "config.yaml")
    assert config["provisioning"]["folders"] == ["/etc/perses/provisioning"]
    assert config["datasource"]["proxy"]["allowed_hosts"] == ["prometheus"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_query_reads_only_catalogue_metrics(name: str) -> None:
    allowed = _catalogue() | DCGM
    queries = _queries(_dashboards()[name])
    assert queries, name
    for expr in queries:
        unknown = _metrics(expr) - allowed
        assert not unknown, f"{name}: {expr} reads {sorted(unknown)}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_queries_group_and_match_only_by_allowlisted_labels(name: str) -> None:
    for expr in _queries(_dashboards()[name]):
        for _, labels in GROUPING.findall(expr):
            for label in filter(None, (part.strip() for part in labels.split(","))):
                assert label in LABEL_ALLOWLIST, f"{name}: {expr} groups by {label}"
        for matchers in SELECTOR.findall(expr):
            for label in MATCHER_LABEL.findall(matchers):
                assert label in LABEL_ALLOWLIST, f"{name}: {expr} matches on {label}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_variable_is_an_identity(name: str) -> None:
    for variable in _dashboards()[name]["spec"].get("variables", []):
        var_name = variable["spec"]["name"]
        assert var_name not in IDENTITY_WORDS
        assert var_name in LABEL_ALLOWLIST, var_name


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_panel_is_laid_out_once(name: str) -> None:
    dashboard = _dashboards()[name]
    refs = [
        item["content"]["$ref"]
        for layout in dashboard["spec"]["layouts"]
        for item in layout["spec"]["items"]
    ]
    assert sorted(refs) == sorted(f"#/spec/panels/{p}" for p in dashboard["spec"]["panels"])
    for layout in dashboard["spec"]["layouts"]:
        for item in layout["spec"]["items"]:
            assert 0 <= item["x"] and item["x"] + item["width"] <= 24


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_dashboards_carry_no_links(name: str) -> None:
    text = (DASHBOARDS / f"{name}.yaml").read_text()
    assert not re.search(r"https?://", text)


def test_the_gpu_dashboard_says_no_data_without_gpu_nodes_and_falls_back_to_zero() -> None:
    gpu = _dashboards()["gpu"]
    queries = _queries(gpu)
    assert queries
    assert all(expr.rstrip().endswith("or vector(0)") for expr in queries), queries
    assert all(_metrics(expr) <= DCGM for expr in queries)
    notes = [
        panel["spec"]["plugin"]["spec"]["text"]
        for panel in gpu["spec"]["panels"].values()
        if panel["spec"]["plugin"]["kind"] == "Markdown"
    ]
    assert any("No GPU nodes" in note and "Compose" in note for note in notes)


def test_sc5_topics_are_covered() -> None:
    dashboards = _dashboards()
    metrics = {name: set().union(*map(_metrics, _queries(d))) for name, d in dashboards.items()}
    assert {"munshi_bot_joins_total", "munshi_join_success_ratio_24h",
            "munshi_canary_consecutive_failures"} <= metrics["joins"]
    assert {"munshi_asr_segment_lag_seconds", "munshi_asr_segment_lag_missing_total"} <= metrics["asr"]
    assert {"munshi_pipeline_pending", "munshi_pipeline_oldest_pending_seconds",
            "munshi_pipeline_failed_24h", "munshi_pipeline_stage_seconds",
            "munshi_erasures_pending"} <= metrics["pipeline"]
    assert {"munshi_intake_smtp_total", "munshi_intake_messages_24h"} <= metrics["intake"]
    assert {"munshi_mail_sent_total"} <= metrics["email"]
