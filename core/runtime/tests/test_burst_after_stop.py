"""A dispatch burst after the earlier bots have left their meetings is admitted up to the cap.

The control plane stops a running bot by asking it to leave (meeting-api publishes a leave command);
the bot leaves and its container or Pod exits on its own. Nothing tells the runtime: its record of
that bot says ``running`` until the runtime observes the exit on the substrate. This file replays
that sequence offline — N bots running at the cap, each exits a moment after its stop, then N new
dispatches arrive together — and asserts all N are admitted once every earlier bot is gone."""
from __future__ import annotations

import threading
import time
from typing import Optional

import pytest

from runtime_kernel import Runtime, WorkloadSpec
from runtime_kernel.__main__ import _start_cap_reconciler
from runtime_kernel.backend import WorkloadHandle
from runtime_kernel.kernel import ClusterCapExceeded
from runtime_kernel.models import RuntimeState
from runtime_kernel.profiles import Runnable

PROFILES = {"meeting-bot": Runnable(image="bot:test")}
CAP = 5
INTERVAL_S = 0.2           # RUNTIME_CAP_RECHECK_S for these tests
LISTING_S = 0.3            # one substrate listing (kubectl get pods / docker ps) takes this long


class LeavingBackend:
    """Bots that leave on request: ``leave(id, after_s)`` makes the workload exit ``after_s`` seconds
    later, as a bot does once it has heard the leave command and left the meeting. Exited workloads
    stay listed (not running) until deleted, as a finished Pod or an exited container does."""

    name = "process"

    def __init__(self) -> None:
        self.started: list[str] = []
        self.exited: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self, workload_id, runnable, env, resources=None) -> WorkloadHandle:
        with self._lock:
            self.started.append(workload_id)
        return WorkloadHandle(id=workload_id, impl=workload_id)

    def leave(self, workload_id: str, after_s: float) -> threading.Timer:
        timer = threading.Timer(after_s, lambda: self._exit(workload_id))
        timer.daemon = True
        timer.start()
        return timer

    def _exit(self, workload_id: str) -> None:
        with self._lock:
            self.exited[workload_id] = 0

    def exit_code(self, h: WorkloadHandle) -> Optional[int]:
        with self._lock:
            return self.exited.get(h.id)

    def terminate(self, h: WorkloadHandle) -> None:
        self._exit(h.id)

    def kill(self, h: WorkloadHandle) -> None:
        self._exit(h.id)

    def cleanup(self, h: WorkloadHandle) -> None:
        self._exit(h.id)

    def list_workload_containers(self) -> list[dict]:
        time.sleep(LISTING_S)
        with self._lock:
            return [{"workload_id": w, "name": w, "running": w not in self.exited}
                    for w in self.started]


@pytest.fixture
def started():
    """Reconcilers a test started; stopped when it ends."""
    reconcilers = []
    yield reconcilers
    for r in reconcilers:
        r.stop()


def production_runtime(backend: LeavingBackend, started: list) -> Runtime:
    """The runtime as ``python -m runtime_kernel`` wires it with a cluster cap: the kernel plus its
    background cap reconciler every ``RUNTIME_CAP_RECHECK_S``."""
    rt = Runtime(backend=backend, profiles=PROFILES, grace_sec=0.1, cluster_cap=CAP,
                 cap_recheck_s=INTERVAL_S)
    started.append(_start_cap_reconciler(rt, INTERVAL_S))
    return rt


def bot(workload_id: str) -> WorkloadSpec:
    return WorkloadSpec(workloadId=workload_id, profile="meeting-bot", env={})


def burst(rt: Runtime, prefix: str, n: int) -> tuple[list[str], list[str]]:
    """``n`` creates released together; returns (admitted ids, refused ids)."""
    admitted: list[str] = []
    refused: list[str] = []
    lock = threading.Lock()
    gate = threading.Barrier(n)

    def create(i: int) -> None:
        wid = f"{prefix}{i}"
        gate.wait()
        try:
            rt.create(bot(wid))
            with lock:
                admitted.append(wid)
        except ClusterCapExceeded:
            with lock:
                refused.append(wid)

    threads = [threading.Thread(target=create, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return admitted, refused


def stop_all_and_let_them_exit(rt: Runtime, backend: LeavingBackend, ids: list[str],
                               settle_s: float) -> None:
    """Each bot leaves 0.1–0.5 s after its stop (staggered); then wait ``settle_s`` more."""
    timers = [backend.leave(wid, 0.1 + 0.1 * i) for i, wid in enumerate(ids)]
    for timer in timers:
        timer.join(timeout=5)
    assert set(backend.exited) >= set(ids)          # every earlier bot has left and exited
    time.sleep(settle_s)


def test_a_burst_after_every_earlier_bot_exited_is_admitted_up_to_the_cap(started):
    backend = LeavingBackend()
    rt = production_runtime(backend, started)
    first, refused = burst(rt, "a", CAP)
    assert (len(first), refused) == (CAP, [])

    # one interval to observe the exits plus one 0.3 s listing, with margin for a loaded machine
    stop_all_and_let_them_exit(rt, backend, first, settle_s=5 * INTERVAL_S)
    second, refused = burst(rt, "b", CAP)

    assert sorted(second) == sorted(f"b{i}" for i in range(CAP)), f"refused: {sorted(refused)}"
    assert all(rt.store.get(w).status.state is RuntimeState.stopped for w in first)


def test_a_later_burst_is_admitted_up_to_the_cap_and_refuses_the_rest(started):
    """26 s passed between the runs in the staging re-check: a burst well after the exits gets the
    cap's worth of slots, and only the creates over it are refused."""
    backend = LeavingBackend()
    rt = production_runtime(backend, started)
    first, _ = burst(rt, "a", CAP)

    stop_all_and_let_them_exit(rt, backend, first, settle_s=10 * INTERVAL_S)
    second, refused = burst(rt, "b", CAP + 2)

    assert len(second) == CAP, f"admitted {len(second)}, refused {len(refused)}"
    assert len(refused) == 2
