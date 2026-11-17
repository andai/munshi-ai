"""The cluster-wide concurrent-bot cap (RUNTIME_MAX_ACTIVE_BOTS): a meeting-bot create over the
cap is refused at once — ClusterCapExceeded, HTTP 429 `capacity_exhausted` — before the backend
starts anything, so overflow never becomes a Pod left Pending. Offline, over a fake backend."""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from runtime_kernel import Runtime, WorkloadSpec
from runtime_kernel.__main__ import _cluster_cap
from runtime_kernel.api import create_app
from runtime_kernel.backend import WorkloadHandle
from runtime_kernel.clock import FakeClock
from runtime_kernel.kernel import CapReconciler, ClusterCapExceeded
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
def test_a_bot_that_exited_on_its_own_frees_its_slot_at_the_next_reconcile(listing):
    """Nothing read the exited bot, so its record still says running and a create at the cap is
    refused from that count; the next reconcile (one listing where the backend has one, then a read
    of each running record not listed live) marks it stopped and the slot is free again."""
    backend = FakeBackend(listing=listing)
    rt = runtime(2, backend)
    rt.create(bot("b1"))
    rt.create(bot("b2"))
    backend.exited["b1"] = 0
    with pytest.raises(ClusterCapExceeded):
        rt.create(bot("b3"))
    assert rt.store.get("b1").status.state is RuntimeState.running   # stale until reconciled

    assert rt.reconcile_cap() is True
    assert rt.store.get("b1").status.state is RuntimeState.stopped
    assert rt.create(bot("b3")).state is RuntimeState.running
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


# ── refusal latency: admission answers from the store; the substrate is read in the background ─────


class SlowListingBackend(FakeBackend):
    """A backend whose substrate listing takes ``list_delay`` seconds, as `kubectl get pods` does on
    a loaded node. ``check`` runs on every substrate read so a test can assert what is held then."""

    def __init__(self, list_delay: float = 0.0, check=None, block: Optional[threading.Event] = None):
        super().__init__(listing=True)
        self.list_delay = list_delay
        self.check = check or (lambda: None)
        self.block = block
        self.list_calls = 0

    def _list(self) -> list[dict]:
        self.check()
        self.list_calls += 1
        if self.block is not None:
            self.block.wait(timeout=10)
        if self.list_delay:
            time.sleep(self.list_delay)
        return super()._list()

    def exit_code(self, h: WorkloadHandle) -> Optional[int]:
        self.check()
        return super().exit_code(h)


def _race(rt: Runtime, n: int, prefix: str = "b"
          ) -> tuple[list[int], list[tuple[int, float]], list[float]]:
    """``n`` concurrent meeting-bot creates; returns (admitted ids, [(refused id, seconds taken)],
    seconds taken by every call)."""
    admitted: list[int] = []
    refused: list[tuple[int, float]] = []
    took: list[float] = []
    lock = threading.Lock()
    gate = threading.Barrier(n)

    def create(i: int) -> None:
        gate.wait()
        t0 = time.monotonic()
        try:
            rt.create(bot(f"{prefix}{i}"))
            with lock:
                admitted.append(i)
        except ClusterCapExceeded:
            with lock:
                refused.append((i, time.monotonic() - t0))
        with lock:
            took.append(time.monotonic() - t0)

    threads = [threading.Thread(target=create, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return admitted, refused, took


def test_no_create_waits_on_the_substrate():
    """A listing that takes 8 s (kubectl while 11 Pods are being created), cap 11, 21 concurrent
    creates with the background reconciler running: 11 admitted, 10 refused, every call within 1 s."""
    backend = SlowListingBackend(list_delay=8.0)
    rt = runtime(11, backend)
    reconciler = CapReconciler(rt, interval_s=0.05).start()
    try:
        admitted, refused, took = _race(rt, 21)
    finally:
        reconciler.stop(timeout=0.1)          # its 8 s listing may still be running; it is a daemon

    assert (len(admitted), len(refused)) == (11, 10)
    assert max(took) < 1.0, f"slowest create took {max(took):.1f}s"
    assert rt.store.count_active(["meeting-bot"]) == 11
    assert sorted(backend.started) == sorted(f"b{i}" for i in admitted)


def test_refusals_at_the_cap_do_not_queue_behind_the_substrate():
    """21 creates against a cap of 5 and a listing that takes 1 s: exactly 5 admitted, and every one
    of the 16 refusals answers within 2 s — no create lists at all."""
    backend = SlowListingBackend(list_delay=1.0)
    rt = runtime(5, backend)

    admitted, refused, _ = _race(rt, 21)

    assert (len(admitted), len(refused)) == (5, 16)
    slowest = max(seconds for _, seconds in refused)
    assert slowest < 2.0, f"slowest refusal took {slowest:.1f}s"
    assert backend.list_calls == 0
    assert rt.store.count_active(["meeting-bot"]) == 5
    assert sorted(backend.started) == sorted(f"b{i}" for i in admitted)


class PeakStore(InMemoryStore):
    """Remembers the highest cap count seen right after any write — the instants it can rise."""

    def __init__(self) -> None:
        super().__init__()
        self.peak = 0
        self._peak_lock = threading.Lock()

    def set(self, record) -> None:
        super().set(record)
        n = self.count_active(["meeting-bot", "adopted"])
        with self._peak_lock:
            self.peak = max(self.peak, n)


def test_reconciler_and_creates_race_without_exceeding_the_cap():
    """5 bots at the cap exit one by one while 21 creates arrive over 1.5 s, the reconciler writes
    `stopped` every 20 ms and a reader lists the workloads throughout: the count never exceeds the
    cap, each freed slot goes to one create, and no exited bot is marked stopped twice."""
    backend = FakeBackend(listing=True)
    store = PeakStore()
    rt = runtime(5, backend, store)
    stopped_events: list[str] = []
    rt.on_event = lambda ev: (stopped_events.append(ev.workloadId)
                              if ev.state is RuntimeState.stopped else None)
    for n in range(5):
        rt.create(bot(f"old{n}"))
    reconciler = CapReconciler(rt, interval_s=0.02).start()
    reading = threading.Event()

    def reader() -> None:
        while not reading.is_set():
            rt.list()

    threading.Thread(target=reader, daemon=True).start()
    exits = [threading.Timer(0.05 + 0.1 * n, backend.exited.__setitem__, (f"old{n}", 0))
             for n in range(5)]
    admitted: list[str] = []
    lock = threading.Lock()

    def create(i: int) -> None:
        time.sleep(0.075 * i)
        try:
            rt.create(bot(f"new{i}"))
            with lock:
                admitted.append(f"new{i}")
        except ClusterCapExceeded:
            pass

    threads = [threading.Thread(target=create, args=(i,)) for i in range(21)]
    try:
        for timer in exits:
            timer.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        reading.set()
        reconciler.stop()

    assert store.peak <= 5
    assert len(admitted) == 5, admitted
    assert rt.store.count_active(["meeting-bot"]) == 5
    assert sorted(stopped_events) == [f"old{n}" for n in range(5)]   # each exit recorded once


def test_no_kubectl_or_callback_under_the_admission_lock():
    """The reconciler's listing, its per-workload exit reads and every lifecycle event (the callback
    POST) run with the admission lock free, while creates take it."""
    held: list[str] = []
    rt_box: list[Runtime] = []

    def lock_must_be_free(what: str):
        def check():
            if rt_box and rt_box[0]._admission_lock.locked():
                held.append(what)
        return check

    backend = SlowListingBackend(check=lock_must_be_free("substrate"))
    rt = runtime(2, backend)
    rt_box.append(rt)
    rt.on_event = lambda ev: lock_must_be_free(f"event {ev.workloadId} {ev.state.value}")()
    rt.create(bot("b1"))
    rt.create(bot("b2"))
    backend.exited["b1"] = 0

    assert rt.reconcile_cap() is True
    assert rt.create(bot("b3")).state is RuntimeState.running     # the reconcile freed b1's slot
    with pytest.raises(ClusterCapExceeded):
        rt.create(bot("b4"))

    assert backend.list_calls >= 1
    assert held == []


def test_admission_never_waits_on_the_substrate():
    """Below and at the cap the count is the store's: a substrate that never answers is never asked
    by a create."""
    never = threading.Event()
    backend = SlowListingBackend(block=never)
    rt = runtime(4, backend)
    done = threading.Event()
    refused: list[str] = []

    def create_six() -> None:
        for n in range(6):
            try:
                rt.create(bot(f"b{n}"))
            except ClusterCapExceeded:
                refused.append(f"b{n}")
        done.set()

    threading.Thread(target=create_six, daemon=True).start()
    finished = done.wait(timeout=5)
    never.set()

    assert finished, "a create waited on the substrate"
    assert refused == ["b4", "b5"]
    assert backend.list_calls == 0


def test_refusal_during_a_reconcile_keeps_the_contract():
    """While the reconciler is inside a listing, a create over the cap is refused at once with the
    sealed runtime.v1 answer: 429 {"detail": "capacity_exhausted"}."""
    release = threading.Event()
    backend = SlowListingBackend(block=release)
    rt = runtime(1, backend)
    client = TestClient(create_app(rt, deliver=lambda ev: None))
    assert client.post("/workloads", json={"workloadId": "b1", "profile": "meeting-bot",
                                           "env": {}}).status_code == 201

    reconciling = threading.Thread(target=rt.reconcile_cap, daemon=True)
    reconciling.start()
    deadline = time.monotonic() + 5
    while backend.list_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend.list_calls == 1                                  # the reconcile is listing

    t0 = time.monotonic()
    r = client.post("/workloads", json={"workloadId": "b2", "profile": "meeting-bot", "env": {}})
    took = time.monotonic() - t0
    release.set()
    reconciling.join(timeout=5)

    assert r.status_code == 429
    assert r.json() == {"detail": "capacity_exhausted"}
    assert took < 1.0
    assert backend.list_calls == 1                                  # the create did not list


def test_concurrent_creates_take_a_slot_freed_by_the_reconciler_once():
    """A slot freed by an exited bot (observed by the reconciler) goes to exactly one of the
    concurrent creates."""
    backend = SlowListingBackend(list_delay=0.2)
    rt = runtime(5, backend, SlowCountStore())
    for n in range(5):
        rt.create(bot(f"old{n}"))
    backend.exited["old0"] = 0
    assert rt.reconcile_cap() is True

    admitted, refused, _ = _race(rt, 20)

    assert (len(admitted), len(refused)) == (1, 19)
    assert rt.store.count_active(["meeting-bot"]) == 5


@pytest.mark.parametrize("raw,value", [(None, 2.0), ("", 2.0), (" 0.5 ", 0.5), ("10", 10.0)])
def test_the_reconcile_interval_env_value(monkeypatch, raw, value):
    from runtime_kernel.__main__ import _cap_recheck_s

    if raw is None:
        monkeypatch.delenv("RUNTIME_CAP_RECHECK_S", raising=False)
    else:
        monkeypatch.setenv("RUNTIME_CAP_RECHECK_S", raw)
    assert _cap_recheck_s() == value


@pytest.mark.parametrize("raw", ["0", "-1", "two", "nan", "inf"])
def test_an_invalid_reconcile_interval_is_fatal_at_boot(monkeypatch, raw):
    from runtime_kernel.__main__ import _cap_recheck_s

    monkeypatch.setenv("RUNTIME_CAP_RECHECK_S", raw)
    with pytest.raises(ValueError, match="RUNTIME_CAP_RECHECK_S"):
        _cap_recheck_s()
