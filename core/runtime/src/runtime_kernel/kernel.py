"""The runtime kernel — orchestrates a workload through the runtime.v1 lifecycle over a Backend,
emitting RuntimeEvents on every transition. `profile` is opaque (P11): the kernel maps it to a
runnable via a registry (policy/config), but the contract never sees the command.

Persistence is via the WorkloadStore port: the (spec, status) pair lives in the store (InMemory by
default, Redis for durability) so the runtime survives a restart. Live backend handles are NOT
serializable, so they live in a process-local map keyed by workloadId; on a fresh process they are
simply absent, and the reloaded statuses describe what was running before the restart.

Quotas (O-RT-2): create() rejects the N+1th active workload for an owner via the store's
count_for_owner.

Cluster cap: with ``cluster_cap`` set, create() refuses a meeting bot at once (ClusterCapExceeded,
HTTP 429 ``capacity_exhausted``) when that many bots are already starting or running, before any
container or Pod exists — overflow is a defined refusal, never a Pod left Pending. The count and the
``starting`` record are written under one lock, so concurrent creates cannot both take the last
slot; the lock is per process, so the cap is exact only with a single runtime replica. Admission
reads the store only — no create ever waits on a substrate listing, an exit read or a callback. The
store learns that a bot exited on its own (a bot leaves its meeting when asked to, then its
container or Pod exits) from the ``CapReconciler``: one background thread per runtime process that,
every ``cap_recheck_s`` while a counted bot is running, lists the substrate and marks the exited bots
``stopped`` outside the lock. A slot frees only once its bot's exit is observed.

Finished workloads: on a backend that offers ``reclaim_finished`` (Kubernetes, where a bare Pod with
restartPolicy Never stays Succeeded/Failed until deleted), a workload observed to have exited is
reclaimed after its record is marked stopped, and ``sweep_finished`` deletes finished ones the
runtime never observed."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .backend import Backend, WorkloadHandle
from .clock import Clock, SystemClock
from .models import RuntimeEvent, RuntimeState, StopReason, WorkloadSpec, WorkloadStatus
from .process_backend import ProcessBackend
from .profiles import Profile, ProfileRegistry, Runnable, default_registry
from .store import (
    InMemoryStore,
    OwnerResolver,
    WorkloadRecord,
    WorkloadStore,
    default_owner,
)


logger = logging.getLogger("runtime_kernel.kernel")

# How often the cap reconciler checks the substrate for counted bots that exited (seconds).
DEFAULT_CAP_RECHECK_S = 2.0


class QuotaExceeded(Exception):
    """Raised by create() when an owner is already at their active-workload cap."""

    def __init__(self, owner: str, cap: int) -> None:
        self.owner = owner
        self.cap = cap
        super().__init__(f"owner {owner!r} at quota cap ({cap})")


class ClusterCapExceeded(QuotaExceeded):
    """Raised by create() when the cluster already runs ``cap`` meeting bots (``cluster_cap``)."""

    def __init__(self, cap: int) -> None:
        self.owner = ""
        self.cap = cap
        Exception.__init__(self, "capacity_exhausted")


# The profiles the cluster cap counts. Agent workers are not bots and do not count. Records that a
# restarted runtime re-adopted from the substrate carry the profile "adopted" (the backend labels
# carry no profile), so they count too: refusing a bot early beats running more than the cap.
CAPPED_PROFILE = "meeting-bot"
_CAP_COUNTED = (CAPPED_PROFILE, "adopted")


class StartFailed(Exception):
    """Raised by create() when the backend could not START the workload (e.g. the docker
    image is absent → a 404 on container create). The honest ``stopped``/``start_failed``
    record is persisted and its RuntimeEvent emitted BEFORE this raises, so ``GET /workloads``
    and the callback stream still tell the truth; the raise carries the backend's own reason
    text so the API answers a non-201 that NAMES the cause instead of a false ``spawned`` 201
    over a workload that never came up."""

    def __init__(self, workload_id: str, reason: str) -> None:
        self.workload_id = workload_id
        self.reason = reason
        super().__init__(reason)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Runtime:
    def __init__(
        self,
        backend: Optional[Backend] = None,
        profiles: Optional[dict | ProfileRegistry] = None,
        on_event: Optional[Callable[[RuntimeEvent], None]] = None,
        grace_sec: float = 5.0,
        store: Optional[WorkloadStore] = None,
        clock: Optional[Clock] = None,
        owner_resolver: OwnerResolver = default_owner,
        owner_quota: Optional[int] = None,
        cluster_cap: Optional[int] = None,
        cap_recheck_s: float = DEFAULT_CAP_RECHECK_S,
    ) -> None:
        self.backend: Backend = backend or ProcessBackend()
        # `profiles` accepts a ProfileRegistry, a plain {name: Runnable|command} dict (legacy/tests),
        # or None (the real default registry). We normalize to a ProfileRegistry.
        self.profiles: ProfileRegistry = _coerce_registry(profiles)
        self.on_event = on_event or (lambda e: None)
        self.grace_sec = grace_sec
        self.store: WorkloadStore = store if store is not None else InMemoryStore()
        self.clock: Clock = clock or SystemClock()
        self.owner_resolver = owner_resolver
        self.owner_quota = owner_quota
        # Cluster-wide concurrent meeting bots; None (or 0) means no cap.
        self.cluster_cap = cluster_cap or None
        self.cap_recheck_s = cap_recheck_s
        # Guards the store's count and the `starting` write — never a substrate call or an event.
        self._admission_lock = threading.Lock()
        # One cap reconcile (listing + exit reads) at a time, whoever calls it.
        self._reconcile_lock = threading.Lock()
        # Serialises the running → stopped write of an exit, so a reconcile and a concurrent read
        # cannot both record (and emit) the same exit. Store only — never held while emitting.
        self._exit_lock = threading.Lock()
        # Live, non-serializable backend handles. Empty on a fresh process (post-restart).
        self._handles: dict[str, WorkloadHandle] = {}

    def _emit(self, workload_id: str, state: RuntimeState, **kw) -> RuntimeEvent:
        ev = RuntimeEvent(workloadId=workload_id, state=state, at=_now(), **kw)
        self.on_event(ev)
        return ev

    def _persist(self, spec: WorkloadSpec, status: WorkloadStatus) -> None:
        self.store.set(WorkloadRecord(spec=spec, status=status, owner=self.owner_resolver(spec)))

    def _record(self, workload_id: str) -> WorkloadRecord:
        record = self.store.get(workload_id)
        if record is None:
            raise KeyError(workload_id)
        return record

    def _handle_for(self, workload_id: str) -> Optional[WorkloadHandle]:
        """The live handle for a workload — re-derived from the substrate via the backend's
        optional ``find`` when the in-process map lost it (a restarted runtime). Without this, a
        post-restart ``stop``/``destroy`` would report success while the real container kept
        running (the orphaned-live-bot defect)."""
        h = self._handles.get(workload_id)
        if h is None:
            finder = getattr(self.backend, "find", None)
            if finder is not None:
                try:
                    h = finder(workload_id)
                except Exception:  # noqa: BLE001 — a failed lookup means "no handle", never a crash
                    h = None
                if h is not None:
                    self._handles[workload_id] = h
        return h

    def adopt(self) -> int:
        """Boot-time re-adoption (the orphaned-live-bot fix): ask the backend for the workload
        containers it spawned that still exist on the substrate, re-attach live handles, and
        re-register any record the (in-memory) store lost across the restart — so
        ``GET /workloads/{id}`` answers truthfully after a runtime recreate instead of 404ing over
        a still-running bot, and stop/destroy reach the real container again.

        A still-running container re-registers as ``running``; an exited one as ``stopped`` with
        its real exit code (EVIDENCE the control plane's lifecycle decisions can act on). Records
        a durable store kept only get their handle re-attached. Returns the number of records
        re-registered. Never raises — adoption must not block the boot."""
        lister = getattr(self.backend, "list_workload_containers", None)
        if lister is None:
            return 0
        try:
            discovered = lister()
        except Exception:  # noqa: BLE001 — discovery failure must not block the boot
            return 0
        adopted = 0
        for info in discovered:
            wid = info.get("workload_id")
            name = info.get("name")
            if not wid or not name:
                continue
            self._handles.setdefault(wid, WorkloadHandle(id=wid, impl=name))
            if self.store.get(wid) is not None:
                continue                       # durable store kept the record — handle was the gap
            spec = WorkloadSpec(workloadId=wid, profile="adopted", env={})
            status = WorkloadStatus(
                workloadId=wid, profile=spec.profile,
                state=RuntimeState.running, backend=self.backend.name,
            )
            if not info.get("running"):
                code = info.get("exit_code")
                status.state = RuntimeState.stopped
                status.exitCode = code
                status.stoppedAt = _now()
                status.stopReason = StopReason.completed if code == 0 else StopReason.failed
            self._persist(spec, status)
            adopted += 1
        return adopted

    # ── runtime.v1 operations ────────────────────────────────────────────────
    def create(self, spec: WorkloadSpec) -> WorkloadStatus:
        # runtime.v1: workloadId is the caller-assigned IDEMPOTENCY KEY (ADR 0027). A create for a
        # workload that is still starting/running is a TOUCH — return the live status unchanged: no
        # respawn (the docker backend's name-conflict path would force-delete the RUNNING container,
        # the copilot-churn defect), no spec overwrite, no quota charge. This check runs BEFORE the
        # profile resolve and the quota gate so a keep-alive touch can never 400/429 an owner at cap.
        # get() reflects a self-exited workload to stopped (and re-derives the handle across a
        # runtime restart via backend.find), so a stale "running" record falls through to the spawn
        # path, where the backend's stale-name replace reclaims the exited container. `starting` is
        # touched too — the truthful read of a concurrent create mid-spawn; a record crashed stuck
        # in `starting` was already unrecoverable-by-create before this (stop/destroy clears it).
        try:
            existing = self.get(spec.workloadId)
        except KeyError:
            existing = None
        if existing is not None and existing.state in (RuntimeState.starting, RuntimeState.running):
            return existing

        profile = self.profiles.get(spec.profile)
        if profile is None:
            raise ValueError(f"unknown profile: {spec.profile!r}")
        runnable = profile.runnable

        # Quota check (O-RT-2): reject the N+1th active workload for this owner.
        if self.owner_quota is not None:
            owner = self.owner_resolver(spec)
            if self.store.count_for_owner(owner) >= self.owner_quota:
                raise QuotaExceeded(owner, self.owner_quota)

        status = WorkloadStatus(
            workloadId=spec.workloadId, profile=spec.profile,
            state=RuntimeState.starting, backend=self.backend.name,
        )
        if self.cluster_cap is not None and spec.profile == CAPPED_PROFILE:
            touched = self._admit_under_cluster_cap(spec, status)
            if touched is not None:
                return touched
        else:
            self._persist(spec, status)
        self._emit(spec.workloadId, RuntimeState.starting)
        try:
            # The profile's base_env is the deployment-wide floor (e.g. meeting-bot's BOT_SPEAKER_*
            # tuning, rendered onto the runtime pod by the chart); the per-workload spec.env is
            # layered on top so an explicit spec value always wins. Without this merge the base_env
            # never reaches the spawned pod and chart-set tuning is dead config (issue #771).
            effective_env = {**profile.base_env, **spec.env}
            # Resource intent must cross the Backend port or it is dead contract: the schema accepts
            # `resources`, and a quota-controlled namespace rejects every container that declares
            # none. The spec's own sizing wins; otherwise the profile's deployment default (the
            # chart's per-class sizing) applies; neither ⇒ None, today's unsized spawn.
            effective_resources = spec.resources or profile.resources
            self._handles[spec.workloadId] = self.backend.start(
                spec.workloadId, runnable, effective_env, effective_resources,
            )
        except Exception as exc:
            # Record the honest terminal state (persist + emit) FIRST — GET /workloads and the
            # callback stream must still see stopped/start_failed — THEN raise so the API answers a
            # non-201 naming the cause. A caught-and-returned status here is a false "spawned" 201
            # over a workload that never came up (#718): the create response must not read as success.
            status.state = RuntimeState.stopped
            status.stopReason = StopReason.start_failed
            status.stoppedAt = _now()
            self._persist(spec, status)
            self._emit(spec.workloadId, RuntimeState.stopped, stopReason=StopReason.start_failed)
            raise StartFailed(spec.workloadId, str(exc)) from exc
        status.state = RuntimeState.running
        status.startedAt = _now()
        status.ports = {}
        self._persist(spec, status)
        self._emit(spec.workloadId, RuntimeState.running, ports={})
        return status

    def _admit_under_cluster_cap(
        self, spec: WorkloadSpec, status: WorkloadStatus,
    ) -> Optional[WorkloadStatus]:
        """Write the ``starting`` record if a cluster-cap slot is free; return the live status for a
        touch of a bot that is already starting or running; raise ClusterCapExceeded otherwise.

        Answers from the store alone, under the admission lock: never a substrate call, so a refusal
        is as fast as a count. Exits reach the store through ``reconcile_cap`` (the CapReconciler)."""
        with self._admission_lock:
            admitted, touched = self._try_admit_locked(spec, status)
        if admitted:
            return touched
        raise ClusterCapExceeded(self.cluster_cap)

    def _try_admit_locked(
        self, spec: WorkloadSpec, status: WorkloadStatus,
    ) -> tuple[bool, Optional[WorkloadStatus]]:
        """Caller holds the admission lock. (True, live status) for a touch — a concurrent create of
        the same id may have won since create()'s touch check, and a touch is never a refusal;
        (True, None) after writing the ``starting`` record into a free slot; (False, None) at the
        cap. Store reads and one write only."""
        current = self.store.get(spec.workloadId)
        if current is not None and current.status.state in (
            RuntimeState.starting, RuntimeState.running,
        ):
            return True, current.status
        if self.store.count_active(_CAP_COUNTED) < self.cluster_cap:
            self._persist(spec, status)
            return True, None
        return False, None

    def reconcile_cap(self) -> bool:
        """Reflect counted bots that exited on their own: one listing where the backend has one, then
        a per-workload read of the ``running`` records not found live. Each read that finds an exit
        persists ``stopped``, emits its event (the callback) and reclaims a finished Pod — all
        without the admission lock. One reconcile at a time: a call while another runs returns at
        once. Returns False without touching the substrate when another reconcile is running or no
        counted bot is ``running``; True after a pass. A failed listing means "read each one"; a
        failed read leaves that record as it was."""
        if not self._reconcile_lock.acquire(blocking=False):
            return False
        try:
            running = [
                r.spec.workloadId for r in self.store.list()
                if r.spec.profile in _CAP_COUNTED and r.status.state == RuntimeState.running
            ]
            if not running:
                return False
            live: set[str] = set()
            lister = getattr(self.backend, "list_workload_containers", None)
            if lister is not None:
                try:
                    live = {i["workload_id"] for i in lister() if i.get("running")}
                except Exception as exc:  # noqa: BLE001 — a failed listing only means "check each one"
                    logger.warning("cap reconcile: listing failed: %s", exc)
                    live = set()
            for workload_id in running:
                if workload_id in live:
                    continue
                try:
                    self.get(workload_id)                   # reflects an exit to `stopped`
                except KeyError:
                    pass
                except Exception as exc:  # noqa: BLE001 — one failed read never stops the pass
                    logger.warning("cap reconcile: reading %s failed: %s", workload_id, exc)
            return True
        finally:
            self._reconcile_lock.release()

    def _reclaim_exited(self, workload_id: str, handle: WorkloadHandle) -> None:
        """Delete a workload the substrate keeps after it exited (a finished Pod). Called only after
        its record is persisted ``stopped``; the backend deletes it only if it has finished. Never
        raises: the periodic sweep is the backstop."""
        reclaim = getattr(self.backend, "reclaim_finished", None)
        if reclaim is None:
            return
        try:
            reclaim(handle)
        except Exception as exc:  # noqa: BLE001 — a failed delete is retried by the sweep
            logger.warning("reclaiming exited workload %s failed: %s", workload_id, exc)
            return
        self._handles.pop(workload_id, None)

    def sweep_finished(self, ttl_s: float) -> int:
        """Delete the finished workloads the backend reports as done for at least ``ttl_s`` seconds
        (``finished_workloads``; Kubernetes only). A record still ``running`` is marked ``stopped``
        with the reported exit code first, so the exit is not lost with the Pod. ``ttl_s`` 0 or
        less ⇒ off. Returns how many were deleted."""
        finder = getattr(self.backend, "finished_workloads", None)
        if ttl_s <= 0 or finder is None:
            return 0
        swept = 0
        for info in finder(older_than_s=ttl_s, now=self.clock.now()):
            workload_id = info["workload_id"]
            record = self.store.get(workload_id)
            if record is not None and record.status.state == RuntimeState.running:
                self._record_exit(record, info["exit_code"])
            try:
                self.backend.cleanup(WorkloadHandle(id=workload_id, impl=info["name"]))
            except Exception as exc:  # noqa: BLE001 — the next sweep tries again
                logger.warning("sweep: deleting %s failed: %s", info["name"], exc)
                continue
            self._handles.pop(workload_id, None)
            swept += 1
        if swept:
            logger.info("swept %d finished workload(s) older than %ss", swept, ttl_s)
        return swept

    def get(self, workload_id: str) -> WorkloadStatus:
        record = self._record(workload_id)
        status = record.status
        # A running record with NO in-process handle (restart) re-derives one from the substrate,
        # so the exit-reflection below stays truthful across a runtime recreate.
        handle = (
            self._handle_for(workload_id)
            if status.state == RuntimeState.running
            else self._handles.get(workload_id)
        )
        # reflect a workload that exited on its own (only observable while we hold a live handle)
        if status.state == RuntimeState.running and handle is not None:
            code = self.backend.exit_code(handle)
            if code is not None and self._record_exit(record, code):
                self._reclaim_exited(workload_id, handle)
        return record.status

    def _record_exit(self, record: WorkloadRecord, code: int) -> bool:
        """Persist and emit the ``stopped`` state of a workload that exited on its own — only if the
        store still has it ``running`` (a concurrent read, reconcile or stop may have got there
        first). Returns whether this call recorded the exit; the event is emitted outside the lock."""
        workload_id = record.spec.workloadId
        with self._exit_lock:
            current = self.store.get(workload_id)
            if current is None or current.status.state != RuntimeState.running:
                if current is not None and current.status is not record.status:
                    record.status = current.status     # the caller returns the stored truth
                return False
            status = record.status
            status.state = RuntimeState.stopped
            status.exitCode = code
            status.stoppedAt = _now()
            status.stopReason = StopReason.completed if code == 0 else StopReason.failed
            self._persist(record.spec, status)
        self._emit(workload_id, RuntimeState.stopped, exitCode=code, stopReason=status.stopReason)
        return True

    def list(self) -> list[WorkloadStatus]:
        return [self.get(r.spec.workloadId) for r in self.store.list()]

    def stop(self, workload_id: str, reason: StopReason = StopReason.stopped) -> WorkloadStatus:
        record = self._record(workload_id)
        status = record.status
        if status.state in (RuntimeState.stopped, RuntimeState.destroyed):
            return status
        status.state = RuntimeState.stopping
        self._persist(record.spec, status)
        self._emit(workload_id, RuntimeState.stopping)
        h = self._handle_for(workload_id)                           # re-derives post-restart handles
        if h is not None:
            self.backend.terminate(h)                               # graceful SIGTERM + grace window
            deadline = time.time() + self.grace_sec
            while self.backend.exit_code(h) is None and time.time() < deadline:
                time.sleep(0.02)
            if self.backend.exit_code(h) is None:
                self.backend.kill(h)                                # force after grace
            code = self.backend.exit_code(h)
        else:
            code = None                                             # no live handle (post-restart stop)
        status.state = RuntimeState.stopped
        status.exitCode = code
        status.stoppedAt = _now()
        status.stopReason = reason
        self._persist(record.spec, status)
        self._emit(workload_id, RuntimeState.stopped, exitCode=code, stopReason=reason)
        return status

    def destroy(self, workload_id: str) -> WorkloadStatus:
        record = self._record(workload_id)
        h = self._handle_for(workload_id)                           # re-derives post-restart handles
        if h is not None:
            self.backend.cleanup(h)   # raises on an unconfirmed reclaim — destroyed is never a lie
        status = record.status
        status.state = RuntimeState.destroyed
        self._persist(record.spec, status)
        self._emit(workload_id, RuntimeState.destroyed)
        return status


class CapReconciler:
    """The background half of the cluster cap: a daemon thread that calls ``runtime.reconcile_cap()``
    every ``interval_s`` seconds, so a bot that exited on the substrate leaves the cap count within
    one interval without any create waiting for it. ``reconcile_cap`` itself skips the substrate
    while no counted bot is running, and never takes the admission lock. A pass that raises is
    logged and the loop goes on. One per runtime process (``__main__`` starts it when a cap is set)."""

    def __init__(self, runtime: "Runtime", interval_s: float = DEFAULT_CAP_RECHECK_S) -> None:
        if not interval_s > 0:
            raise ValueError(f"the cap reconcile interval must be more than 0 s, got {interval_s!r}")
        self.runtime = runtime
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "CapReconciler":
        if not self.alive:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="cap-reconciler", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the loop to end and wait up to ``timeout`` for a pass in progress to finish."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.runtime.reconcile_cap()
            except Exception as exc:  # noqa: BLE001 — a failed pass must not end the loop
                logger.warning("cap reconcile pass failed: %s", exc)


def _coerce_registry(profiles) -> ProfileRegistry:
    """Normalize the `profiles` arg into a ProfileRegistry.

    None → the real default registry (meeting-bot + agent).
    ProfileRegistry → used as-is.
    dict → a registry where each value is a Runnable (a bare command list is wrapped)."""
    if profiles is None:
        return default_registry()
    if isinstance(profiles, ProfileRegistry):
        return profiles
    resolved: dict[str, object] = {}
    for name, value in profiles.items():
        # A full Profile carries the deployment defaults (timeouts, base env, per-class sizing); a
        # bare Runnable or command list is the shorthand for "how to run it, nothing configured".
        if isinstance(value, (Profile, Runnable)):
            resolved[name] = value
        elif isinstance(value, list):
            resolved[name] = Runnable(command=value)
        else:
            raise TypeError(
                f"profile {name!r}: expected Profile, Runnable or command list, got {type(value)}"
            )
    return ProfileRegistry(resolved)
