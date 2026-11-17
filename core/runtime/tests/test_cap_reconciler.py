"""The cluster-cap reconciler: one background thread per runtime process that, every
RUNTIME_CAP_RECHECK_S while a counted bot is running, reflects the bots that exited on the substrate
into the store (``stopped``), so admission can answer from memory alone. Offline, over fakes."""
from __future__ import annotations

import threading
import time
from typing import Optional

import pytest

from runtime_kernel import Runtime, WorkloadSpec
from runtime_kernel.backend import WorkloadHandle
from runtime_kernel.kernel import CapReconciler
from runtime_kernel.models import RuntimeState
from runtime_kernel.profiles import Runnable

PROFILES = {"meeting-bot": Runnable(image="bot:test"), "agent": Runnable(image="agent:test")}


class Backend:
    """Bots that run until told they exited; a listing that can be slowed, blocked or broken."""

    name = "k8s"

    def __init__(self) -> None:
        self.started: list[str] = []
        self.exited: dict[str, int] = {}
        self.reclaimed: list[str] = []
        self.list_calls = 0
        self.listing_now = 0
        self.max_listing_at_once = 0
        self.list_delay = 0.0
        self.list_error: Optional[Exception] = None
        self.on_read = lambda: None
        self._lock = threading.Lock()

    def start(self, workload_id, runnable, env, resources=None) -> WorkloadHandle:
        self.started.append(workload_id)
        return WorkloadHandle(id=workload_id, impl=workload_id)

    def exit_code(self, h: WorkloadHandle) -> Optional[int]:
        self.on_read()
        return self.exited.get(h.id)

    def terminate(self, h: WorkloadHandle) -> None:
        self.exited.setdefault(h.id, 0)

    def kill(self, h: WorkloadHandle) -> None:
        self.exited.setdefault(h.id, 137)

    def cleanup(self, h: WorkloadHandle) -> None:
        self.exited.setdefault(h.id, 0)

    def reclaim_finished(self, h: WorkloadHandle) -> None:
        self.reclaimed.append(h.id)

    def list_workload_containers(self) -> list[dict]:
        with self._lock:
            self.list_calls += 1
            self.listing_now += 1
            self.max_listing_at_once = max(self.max_listing_at_once, self.listing_now)
        try:
            self.on_read()
            if self.list_delay:
                time.sleep(self.list_delay)
            if self.list_error is not None:
                raise self.list_error
            return [{"workload_id": w, "name": w, "running": w not in self.exited}
                    for w in self.started]
        finally:
            with self._lock:
                self.listing_now -= 1


def make(cap: int = 3, interval_s: float = 0.05) -> tuple[Runtime, Backend, CapReconciler]:
    backend = Backend()
    rt = Runtime(backend=backend, profiles=PROFILES, grace_sec=0.1, cluster_cap=cap,
                 cap_recheck_s=interval_s)
    return rt, backend, CapReconciler(rt, interval_s=interval_s)


def bot(workload_id: str) -> WorkloadSpec:
    return WorkloadSpec(workloadId=workload_id, profile="meeting-bot", env={})


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_exited_bots_are_marked_stopped_within_one_interval():
    rt, backend, reconciler = make(interval_s=0.1)
    events = []
    rt.on_event = events.append
    for n in range(3):
        rt.create(bot(f"b{n}"))
    reconciler.start()
    try:
        backend.exited["b0"] = 0
        backend.exited["b2"] = 1
        t0 = time.monotonic()
        assert wait_until(lambda: rt.store.count_active(["meeting-bot"]) == 1, timeout=1.0)
        took = time.monotonic() - t0
    finally:
        reconciler.stop()

    assert took < 0.1 + 0.05, f"took {took:.3f}s for a 0.1 s interval"
    states = {w: rt.store.get(w).status for w in ("b0", "b1", "b2")}
    assert states["b1"].state is RuntimeState.running
    assert (states["b0"].state, states["b0"].exitCode, states["b0"].stopReason.value) == (
        RuntimeState.stopped, 0, "completed")
    assert (states["b2"].state, states["b2"].exitCode, states["b2"].stopReason.value) == (
        RuntimeState.stopped, 1, "failed")
    stopped = [e.workloadId for e in events if e.state is RuntimeState.stopped]
    assert sorted(stopped) == ["b0", "b2"]                    # the callback, once each
    assert sorted(backend.reclaimed) == ["b0", "b2"]          # finished Pods deleted, as in 10-11


def test_one_listing_at_a_time():
    """A pass that takes longer than the interval is never overlapped, by the thread or by a
    concurrent call."""
    rt, backend, reconciler = make(interval_s=0.01)
    rt.create(bot("b0"))
    backend.list_delay = 0.1
    reconciler.start()
    try:
        callers = [threading.Thread(target=rt.reconcile_cap) for _ in range(5)]
        for t in callers:
            t.start()
        for t in callers:
            t.join(timeout=2)
        time.sleep(0.3)
    finally:
        reconciler.stop()

    assert backend.list_calls >= 2
    assert backend.max_listing_at_once == 1


def test_the_admission_lock_is_free_while_listing_and_emitting():
    rt, backend, reconciler = make(interval_s=0.02)
    held: list[str] = []
    backend.on_read = lambda: held.append("substrate") if rt._admission_lock.locked() else None
    rt.on_event = lambda ev: held.append(f"event {ev.workloadId}") if rt._admission_lock.locked() else None
    for n in range(3):
        rt.create(bot(f"b{n}"))
    reconciler.start()
    try:
        backend.exited["b1"] = 0
        assert wait_until(lambda: rt.store.get("b1").status.state is RuntimeState.stopped)
    finally:
        reconciler.stop()
    assert held == []


def test_it_idles_while_no_counted_bot_is_running():
    """No listing while nothing counted runs (only an agent worker, or nothing at all); the first
    running bot wakes it."""
    rt, backend, reconciler = make(interval_s=0.01)
    rt.create(WorkloadSpec(workloadId="a0", profile="agent", env={}))
    reconciler.start()
    try:
        time.sleep(0.2)
        assert backend.list_calls == 0
        rt.create(bot("b0"))
        assert wait_until(lambda: backend.list_calls > 0, timeout=1.0)
        backend.exited["b0"] = 0
        assert wait_until(lambda: rt.store.get("b0").status.state is RuntimeState.stopped)
        settled = backend.list_calls
        time.sleep(0.2)
        assert backend.list_calls == settled                   # idle again: nothing running
    finally:
        reconciler.stop()


def test_it_survives_a_failing_listing():
    """A listing that raises means "read each running record"; a read that raises leaves that record
    as it was; the thread keeps going and the next pass catches up."""
    rt, backend, reconciler = make(interval_s=0.02)
    for n in range(2):
        rt.create(bot(f"b{n}"))
    backend.list_error = RuntimeError("kubectl: connection refused")
    reconciler.start()
    try:
        backend.exited["b0"] = 0
        assert wait_until(lambda: rt.store.get("b0").status.state is RuntimeState.stopped)

        broken = {"on": True}

        def read() -> None:
            if broken["on"]:
                raise RuntimeError("kubectl timed out")
        backend.on_read = read
        backend.exited["b1"] = 0
        calls = backend.list_calls
        assert wait_until(lambda: backend.list_calls >= calls + 3)
        assert rt.store.get("b1").status.state is RuntimeState.running    # unread: unchanged
        broken["on"] = False
        assert wait_until(lambda: rt.store.get("b1").status.state is RuntimeState.stopped)
        assert reconciler.alive
    finally:
        reconciler.stop()


def test_it_stops_cleanly_on_shutdown():
    rt, backend, reconciler = make(interval_s=0.05)
    rt.create(bot("b0"))
    reconciler.start()
    assert wait_until(lambda: backend.list_calls > 0)
    t0 = time.monotonic()
    reconciler.stop(timeout=2)
    assert time.monotonic() - t0 < 0.5
    assert not reconciler.alive
    calls = backend.list_calls
    time.sleep(0.15)
    assert backend.list_calls == calls
    reconciler.stop()                                          # a second stop is a no-op


def test_the_production_wiring_starts_one_reconciler_only_with_a_cap(monkeypatch):
    from runtime_kernel.__main__ import _start_cap_reconciler

    rt, _, _ = make(cap=3)
    started = _start_cap_reconciler(rt, 0.05)
    try:
        assert isinstance(started, CapReconciler) and started.alive
        assert started.interval_s == 0.05
    finally:
        started.stop()

    uncapped = Runtime(backend=Backend(), profiles=PROFILES, cluster_cap=None)
    assert _start_cap_reconciler(uncapped, 0.05) is None


@pytest.mark.parametrize("interval", [0, -1])
def test_a_reconciler_needs_a_positive_interval(interval):
    rt, _, _ = make()
    with pytest.raises(ValueError):
        CapReconciler(rt, interval_s=interval)
