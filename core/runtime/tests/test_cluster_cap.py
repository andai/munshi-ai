"""The cluster-wide concurrent-bot cap (RUNTIME_MAX_ACTIVE_BOTS): a meeting-bot create over the
cap is refused at once — ClusterCapExceeded, HTTP 429 `capacity_exhausted` — before the backend
starts anything, so overflow never becomes a Pod left Pending. Offline, over a fake backend."""
from __future__ import annotations

import threading
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from runtime_kernel import Runtime, WorkloadSpec
from runtime_kernel.__main__ import _cluster_cap
from runtime_kernel.api import create_app
from runtime_kernel.backend import WorkloadHandle
from runtime_kernel.kernel import ClusterCapExceeded
from runtime_kernel.models import RuntimeState, WorkloadStatus
from runtime_kernel.profiles import Runnable
from runtime_kernel.store import InMemoryStore, WorkloadRecord


class FakeBackend:
    """Starts nothing; remembers what it was asked to start and which workloads have exited."""

    name = "process"

    def __init__(self, start_delay: float = 0.0, listing: bool = False) -> None:
        self.started: list[str] = []
        self.exited: dict[str, int] = {}
        self.start_delay = start_delay
        self._lock = threading.Lock()
        if listing:
            self.list_workload_containers = self._list

    def start(self, workload_id, runnable, env, resources=None) -> WorkloadHandle:
        if self.start_delay:
            time.sleep(self.start_delay)
        with self._lock:
            self.started.append(workload_id)
        return WorkloadHandle(id=workload_id, impl=workload_id)

    def exit_code(self, h: WorkloadHandle) -> Optional[int]:
        return self.exited.get(h.id)

    def terminate(self, h: WorkloadHandle) -> None:
        self.exited.setdefault(h.id, 0)

    def kill(self, h: WorkloadHandle) -> None:
        self.exited.setdefault(h.id, 137)

    def cleanup(self, h: WorkloadHandle) -> None:
        self.exited.setdefault(h.id, 0)

    def _list(self) -> list[dict]:
        return [
            {"workload_id": w, "name": w, "running": w not in self.exited}
            for w in self.started
        ]


PROFILES = {"meeting-bot": Runnable(image="bot:test"), "agent": Runnable(image="agent:test")}


class SlowCountStore(InMemoryStore):
    """Counting takes a while, as a scan of a real store does: the window a second create would
    use to take the same last slot if the count and the write were not under one lock."""

    def count_active(self, profiles) -> int:
        n = super().count_active(profiles)
        time.sleep(0.01)
        return n


def runtime(cap: Optional[int], backend: Optional[FakeBackend] = None, store=None) -> Runtime:
    return Runtime(backend=backend or FakeBackend(), profiles=PROFILES, grace_sec=0.1,
                   cluster_cap=cap, store=store)


def bot(workload_id: str) -> WorkloadSpec:
    return WorkloadSpec(workloadId=workload_id, profile="meeting-bot", env={})


def agent(workload_id: str) -> WorkloadSpec:
    return WorkloadSpec(workloadId=workload_id, profile="agent", env={})


def test_the_create_over_the_cap_is_refused_before_anything_starts():
    backend = FakeBackend()
    rt = runtime(2, backend)
    rt.create(bot("b1"))
    rt.create(bot("b2"))

    with pytest.raises(ClusterCapExceeded) as refused:
        rt.create(bot("b3"))

    assert refused.value.cap == 2 and str(refused.value) == "capacity_exhausted"
    assert backend.started == ["b1", "b2"]          # no container, no Pod for the refused bot
    assert rt.store.get("b3") is None               # and no record either


def test_the_api_answers_429_capacity_exhausted():
    events = []
    client = TestClient(create_app(runtime(2), deliver=events.append))
    for wid in ("b1", "b2"):
        assert client.post("/workloads", json={"workloadId": wid, "profile": "meeting-bot",
                                               "env": {}}).status_code == 201

    r = client.post("/workloads", json={"workloadId": "b3", "profile": "meeting-bot", "env": {}})

    assert r.status_code == 429
    assert r.json() == {"detail": "capacity_exhausted"}
    assert not [e for e in events if e.workloadId == "b3"]    # no lifecycle event for it


def test_agent_workers_are_not_counted_and_never_refused():
    rt = runtime(1)
    rt.create(bot("b1"))
    for n in range(5):
        assert rt.create(agent(f"a{n}")).state is RuntimeState.running
    with pytest.raises(ClusterCapExceeded):
        rt.create(bot("b2"))


@pytest.mark.parametrize("end", ["stop", "destroy"])
def test_a_stopped_bot_frees_its_slot(end):
    rt = runtime(1)
    rt.create(bot("b1"))
    getattr(rt, end)("b1")
    assert rt.create(bot("b2")).state is RuntimeState.running


@pytest.mark.parametrize("listing", [False, True])
def test_a_bot_that_exited_on_its_own_frees_its_slot(listing):
    """Nothing read the exited bot, so its record still says running; at the cap the kernel checks
    the substrate (one listing where the backend has one) before refusing."""
    backend = FakeBackend(listing=listing)
    rt = runtime(2, backend)
    rt.create(bot("b1"))
    rt.create(bot("b2"))
    backend.exited["b1"] = 0
    assert rt.store.get("b1").status.state is RuntimeState.running   # stale until read

    assert rt.create(bot("b3")).state is RuntimeState.running
    assert rt.store.get("b1").status.state is RuntimeState.stopped
    with pytest.raises(ClusterCapExceeded):
        rt.create(bot("b4"))


def test_a_keep_alive_touch_of_a_running_bot_is_never_refused():
    backend = FakeBackend()
    rt = runtime(1, backend)
    rt.create(bot("b1"))

    touched = rt.create(bot("b1"))

    assert touched.state is RuntimeState.running
    assert backend.started == ["b1"]


def test_concurrent_creates_admit_exactly_the_cap():
    """The count and the `starting` record are written under one lock: 20 concurrent creates
    against a cap of 5 admit exactly 5, with a slow count and a slow start."""
    backend = FakeBackend(start_delay=0.05)
    rt = runtime(5, backend, SlowCountStore())
    admitted, refused = [], []
    lock = threading.Lock()
    gate = threading.Barrier(20)

    def create(n: int) -> None:
        gate.wait()
        try:
            rt.create(bot(f"b{n}"))
            with lock:
                admitted.append(n)
        except ClusterCapExceeded:
            with lock:
                refused.append(n)

    threads = [threading.Thread(target=create, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert (len(admitted), len(refused)) == (5, 15)
    assert sorted(backend.started) == sorted(f"b{n}" for n in admitted)
    assert rt.store.count_active(["meeting-bot"]) == 5


def test_records_re_adopted_after_a_restart_count():
    """A restarted runtime re-adopts live bots as profile `adopted`; they hold slots too."""
    rt = runtime(1)
    rt.store.set(WorkloadRecord(
        spec=WorkloadSpec(workloadId="old", profile="adopted", env={}),
        status=WorkloadStatus(workloadId="old", profile="adopted", state=RuntimeState.running,
                              backend="process"),
        owner="",
    ))
    rt._handles["old"] = WorkloadHandle(id="old", impl="old")     # still running on the substrate
    with pytest.raises(ClusterCapExceeded):
        rt.create(bot("b1"))


@pytest.mark.parametrize("cap", [None, 0])
def test_no_cap_admits_everything(cap):
    rt = runtime(cap)
    for n in range(25):
        rt.create(bot(f"b{n}"))
    assert rt.store.count_active(["meeting-bot"]) == 25


@pytest.mark.parametrize("raw,cap", [(None, None), ("", None), ("0", None), (" 5 ", 5),
                                     ("200", 200)])
def test_the_env_value(monkeypatch, raw, cap):
    if raw is None:
        monkeypatch.delenv("RUNTIME_MAX_ACTIVE_BOTS", raising=False)
    else:
        monkeypatch.setenv("RUNTIME_MAX_ACTIVE_BOTS", raw)
    assert _cluster_cap() == cap


@pytest.mark.parametrize("raw", ["-1", "five", "2.5"])
def test_an_invalid_env_value_is_fatal_at_boot(monkeypatch, raw):
    monkeypatch.setenv("RUNTIME_MAX_ACTIVE_BOTS", raw)
    with pytest.raises(ValueError, match="RUNTIME_MAX_ACTIVE_BOTS"):
        _cluster_cap()
