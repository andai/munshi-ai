"""Finished bot Pods are deleted. A bare Pod with restartPolicy Never stays `Succeeded`/`Failed`
until someone deletes it, so the runtime does: once, when it observes the exit, and in a periodic
sweep of `runtime.managed=true` Pods that finished more than RUNTIME_FINISHED_POD_TTL_S ago
(default 300; 0 = off). A running Pod is never deleted. Offline: kubectl is a fake."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import pytest

from runtime_kernel import Runtime, WorkloadSpec
from runtime_kernel import k8s_backend
from runtime_kernel.clock import FakeClock
from runtime_kernel.k8s_backend import K8sBackend
from runtime_kernel.models import RuntimeState, StopReason
from runtime_kernel.profiles import Runnable

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def _iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pod(wid: str, phase: str, finished_ago: Optional[float] = None, exit_code: int = 0) -> dict:
    status: dict = {"phase": phase}
    if finished_ago is not None:
        status["containerStatuses"] = [{"state": {"terminated": {
            "exitCode": exit_code, "finishedAt": _iso(finished_ago)}}}]
    return {
        "metadata": {"name": f"munshi-{wid}", "creationTimestamp": _iso(3600),
                     "labels": {"runtime.managed": "true", "runtime.workload_id": wid}},
        "status": status,
    }


class FakeCluster:
    """Answers the kubectl calls the backend makes from a dict of Pods; records every delete."""

    def __init__(self, pods: list[dict]) -> None:
        self.pods = {p["metadata"]["name"]: p for p in pods}
        self.deleted: list[str] = []
        self.list_selectors: list[str] = []

    def __call__(self, *args: str, check: bool = True, stdin=None):
        import subprocess

        def done(code: int = 0, out: str = "", err: str = ""):
            return subprocess.CompletedProcess(["kubectl", *args], code, stdout=out, stderr=err)

        if args[:2] == ("get", "pods"):
            self.list_selectors.append(args[args.index("-l") + 1])
            return done(out=json.dumps({"items": list(self.pods.values())}))
        if args[:2] == ("get", "pod"):
            pod = self.pods.get(args[2])
            if pod is None:
                return done(1, err=f'Error from server (NotFound): pods "{args[2]}" not found')
            return done(out=json.dumps(pod) if "json" in args else f"pod/{args[2]}")
        if args[:2] == ("delete", "pod"):
            self.deleted.append(args[2])
            self.pods.pop(args[2], None)
            return done()
        if args[:2] == ("create", "-f"):
            pod = json.loads(stdin)
            name = pod["metadata"]["name"]
            self.pods[name] = {**pod, "status": {"phase": "Running"}}
            return done()
        raise AssertionError(f"unexpected kubectl call {args}")


@pytest.fixture
def cluster(monkeypatch):
    def install(pods: list[dict]) -> FakeCluster:
        fake = FakeCluster(pods)
        monkeypatch.setattr(k8s_backend, "_kubectl", fake)
        return fake
    return install


def _runtime(clock: Optional[FakeClock] = None) -> Runtime:
    return Runtime(backend=K8sBackend(), profiles={"meeting-bot": Runnable(image="bot:test")},
                   grace_sec=0.1, clock=clock or FakeClock(NOW))


def _bot(wid: str) -> WorkloadSpec:
    return WorkloadSpec(workloadId=wid, profile="meeting-bot", env={})


# ── on observation ────────────────────────────────────────────────────────────────────────────────


def test_an_exited_pod_observed_by_get_is_deleted_once(cluster):
    fake = cluster([])
    rt = _runtime()
    rt.create(_bot("b1"))
    fake.pods["munshi-b1"] = _pod("b1", "Succeeded", finished_ago=1)

    first = rt.get("b1")
    rt.get("b1")
    rt.list()

    assert first.state is RuntimeState.stopped and first.stopReason is StopReason.completed
    assert fake.deleted == ["munshi-b1"]
    rt.destroy("b1")                                         # destroy after the reclaim still works
    assert rt.get("b1").state is RuntimeState.destroyed
    assert fake.deleted == ["munshi-b1"]


def test_a_failed_pod_keeps_its_exit_code_in_the_record(cluster):
    fake = cluster([])
    rt = _runtime()
    rt.create(_bot("b1"))
    fake.pods["munshi-b1"] = _pod("b1", "Failed", finished_ago=1, exit_code=3)

    status = rt.get("b1")

    assert (status.state, status.exitCode, status.stopReason) == (
        RuntimeState.stopped, 3, StopReason.failed)
    assert fake.deleted == ["munshi-b1"]


def test_a_running_pod_is_never_reclaimed(cluster):
    fake = cluster([_pod("b1", "Running")])
    from runtime_kernel.backend import WorkloadHandle

    K8sBackend().reclaim_finished(WorkloadHandle(id="b1", impl="munshi-b1"))

    assert fake.deleted == []


# ── the periodic sweep ────────────────────────────────────────────────────────────────────────────


def test_the_sweep_deletes_only_managed_finished_pods_older_than_the_ttl(cluster):
    fake = cluster([
        _pod("old-done", "Succeeded", finished_ago=400),
        _pod("old-failed", "Failed", finished_ago=301, exit_code=137),
        _pod("young-done", "Succeeded", finished_ago=60),
        _pod("running", "Running"),
        _pod("pending", "Pending"),
    ])
    rt = _runtime()

    swept = rt.sweep_finished(300)

    assert swept == 2
    assert sorted(fake.deleted) == ["munshi-old-done", "munshi-old-failed"]
    assert fake.list_selectors == ["runtime.managed=true"]          # managed Pods only
    assert set(fake.pods) == {"munshi-young-done", "munshi-running", "munshi-pending"}


def test_a_finished_pod_without_a_finish_time_ages_from_its_creation(cluster):
    fake = cluster([_pod("no-status", "Succeeded")])                 # created 3600 s ago
    assert _runtime().sweep_finished(300) == 1
    assert fake.deleted == ["munshi-no-status"]


def test_the_sweep_records_the_exit_before_deleting(cluster):
    """A record still `running` (nothing read it) is marked stopped with the Pod's exit code first,
    so the exit is not lost with the Pod."""
    fake = cluster([])
    rt = _runtime()
    rt.create(_bot("b1"))
    fake.pods["munshi-b1"] = _pod("b1", "Failed", finished_ago=900, exit_code=2)
    assert rt.store.get("b1").status.state is RuntimeState.running

    rt.sweep_finished(300)

    record = rt.store.get("b1").status
    assert (record.state, record.exitCode) == (RuntimeState.stopped, 2)
    assert fake.deleted == ["munshi-b1"]


def test_a_zero_ttl_turns_the_sweep_off(cluster):
    fake = cluster([_pod("old-done", "Succeeded", finished_ago=9999)])
    assert _runtime().sweep_finished(0) == 0
    assert fake.deleted == [] and fake.list_selectors == []


# ── wiring in __main__ ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,ttl", [(None, 300), ("", 300), ("0", 0), (" 900 ", 900)])
def test_the_ttl_env_value(monkeypatch, raw, ttl):
    from runtime_kernel.__main__ import _finished_pod_ttl_s

    if raw is None:
        monkeypatch.delenv("RUNTIME_FINISHED_POD_TTL_S", raising=False)
    else:
        monkeypatch.setenv("RUNTIME_FINISHED_POD_TTL_S", raw)
    assert _finished_pod_ttl_s() == ttl


@pytest.mark.parametrize("raw", ["-1", "5m", "1.5"])
def test_an_invalid_ttl_is_fatal_at_boot(monkeypatch, raw):
    from runtime_kernel.__main__ import _finished_pod_ttl_s

    monkeypatch.setenv("RUNTIME_FINISHED_POD_TTL_S", raw)
    with pytest.raises(ValueError, match="RUNTIME_FINISHED_POD_TTL_S"):
        _finished_pod_ttl_s()


class _Recorder:
    def __init__(self) -> None:
        import threading

        self.calls: list[int] = []
        self.called = threading.Event()

    def sweep_finished(self, ttl_s: int) -> int:
        self.calls.append(ttl_s)
        self.called.set()
        return 0


class _Backend:
    def __init__(self, name: str) -> None:
        self.name = name


def test_the_sweep_thread_runs_on_kubernetes_only():
    from runtime_kernel.__main__ import _start_finished_sweep

    rec = _Recorder()
    thread = _start_finished_sweep(rec, _Backend("k8s"), 300, interval_s=0.01)
    assert thread is not None and thread.daemon
    assert rec.called.wait(timeout=5) and rec.calls[0] == 300

    assert _start_finished_sweep(_Recorder(), _Backend("docker"), 300) is None
    assert _start_finished_sweep(_Recorder(), _Backend("process"), 300) is None


def test_no_sweep_thread_with_a_zero_ttl():
    from runtime_kernel.__main__ import _start_finished_sweep

    rec = _Recorder()
    assert _start_finished_sweep(rec, _Backend("k8s"), 0, interval_s=0.01) is None
    assert rec.calls == []
