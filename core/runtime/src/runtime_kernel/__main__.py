"""``python -m runtime_kernel`` — the production runtime API (P4 compose CMD).

Serves ``runtime_kernel.api.create_app(Runtime(backend=<env-selected>, profiles=default_registry()))``
— the runtime.v1 operation surface that spawns bot/agent workloads. The backend is chosen by
``RUNTIME_BACKEND`` (default ``docker``): ``docker`` talks to the host socket API (compose mounts
``/var/run/docker.sock``), ``k8s`` spawns Pods via kubectl under the runtime's ServiceAccount/RBAC
(deploy/helm), ``process`` runs child processes. Images come from env (BROWSER_IMAGE / AGENT_IMAGE).

Exposed via ``app`` (PEP 562, built on first access) so ``uvicorn runtime_kernel.api:app`` /
``python -m runtime_kernel`` both resolve it without constructing the app at mere import time.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("runtime_kernel.dispatch")


def _http_dispatch(request: dict) -> dict:
    """The scheduler's real dispatch: fire the job's HTTP call when due. A 5xx or a connection error
    raises ``DispatchError`` (retryable — the scheduler backs off and retries); a 2xx completes the
    job (a cron job then re-arms); a 4xx is logged and completed WITHOUT retry (a malformed body won't
    fix itself — a recurring routine simply tries again on its next cron tick)."""
    from .scheduler import DispatchError

    body = request.get("body")
    data = None
    if body is not None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
    headers = {"Content-Type": "application/json", **(request.get("headers") or {})}
    req = urllib.request.Request(
        request["url"], data=data, headers=headers, method=request.get("method", "POST"),
    )
    try:
        with urllib.request.urlopen(req, timeout=request.get("timeout", 30)) as r:
            return {"status_code": r.status}
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            raise DispatchError(f"{request['url']} -> {e.code}") from e
        logger.warning("schedule dispatch %s -> %s (not retried)", request["url"], e.code)
        return {"status_code": e.code, "error": e.reason}
    except urllib.error.URLError as e:  # connection refused / DNS — retryable
        raise DispatchError(f"{request['url']} unreachable: {e.reason}") from e


def _build_scheduler():
    """Construct the durable cron over REDIS_URL, or None when no redis is configured (the API then
    answers 503 on /schedule — honest, P18). Real redis client; SystemClock; the HTTP dispatch above."""
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    import redis as redis_lib

    from .scheduler import Scheduler

    # #528: hardened Redis client (mirrors gateway/meeting-api) — bounded socket + connect timeouts,
    # keepalive, and periodic health checks so a Redis outage makes the scheduler tick raise (caught
    # per-tick) and /health go red, instead of the tick thread hanging on a dead socket forever.
    client = redis_lib.from_url(
        redis_url, decode_responses=True,
        socket_timeout=10, socket_connect_timeout=5, socket_keepalive=True,
        health_check_interval=30, retry_on_timeout=True,
    )
    return Scheduler(client, dispatch=_http_dispatch)


def _start_ticker(scheduler) -> None:
    """Run the scheduler's tick() loop in a daemon thread (a real deployment loops tick on an
    interval; the eval calls tick() explicitly under a FakeClock). Recovers orphans on startup."""
    interval = float(os.getenv("SCHED_TICK_SEC", "5"))
    try:
        recovered = scheduler.recover_orphans()
        if recovered:
            logger.info("scheduler recovered %d orphaned job(s)", recovered)
    except Exception as e:  # noqa: BLE001 — never let startup recovery crash the boot
        logger.warning("scheduler orphan recovery failed: %s", e)

    def _loop() -> None:
        while True:
            try:
                scheduler.tick()
            except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
                logger.warning("scheduler tick error: %s", e)
            time.sleep(interval)

    threading.Thread(target=_loop, name="scheduler-tick", daemon=True).start()


def _build_backend():
    """Select the spawn backend from ``RUNTIME_BACKEND`` (default ``docker``). compose/desktop run
    ``docker`` (host socket API); a k8s deployment runs ``k8s`` (spawns Pods via kubectl under the
    runtime's ServiceAccount/RBAC — see deploy/helm runtime RBAC). ``process`` is the no-container
    fallback. Same Backend port across all three, so the runtime.v1 lifecycle is identical."""
    kind = os.getenv("RUNTIME_BACKEND", "docker").strip().lower()
    if kind == "k8s":
        from .k8s_backend import K8sBackend

        # Namespace is injected via the downward API (POD_NAMESPACE); None ⇒ kubectl's current ns.
        return K8sBackend(namespace=os.getenv("POD_NAMESPACE") or None)
    if kind == "process":
        from .process_backend import ProcessBackend

        return ProcessBackend()
    from .docker_backend import DockerBackend

    return DockerBackend()


def _kernel_grace_sec() -> float:
    """The kernel's stop-poll window, derived from the SAME env the backends' SIGTERM grace reads
    (RUNTIME_STOP_GRACE_SEC, default 30) plus a small margin. Without this wiring the kernel kept
    its 5.0 constructor default: on k8s, ``terminate`` returns immediately (``--wait=false``), the
    pod phase stays Running through the graceful deletion, so ``Runtime.stop()`` force-killed at
    t≈5 — the 30s grace and the bot's leave watchdog were dead letters. The margin keeps the
    kernel's force-kill strictly AFTER the substrate's own grace elapses."""
    try:
        backend_grace = max(1.0, float(os.getenv("RUNTIME_STOP_GRACE_SEC", "30")))
    except ValueError:
        backend_grace = 30.0
    return backend_grace + 5.0


def _cluster_cap() -> int | None:
    """``RUNTIME_MAX_ACTIVE_BOTS``: how many meeting bots may be starting or running at once.
    Unset, empty or 0 ⇒ no cap. Anything else that is not a positive integer is fatal at boot — a
    cap silently dropped is overflow left Pending instead of refused."""
    raw = os.getenv("RUNTIME_MAX_ACTIVE_BOTS", "").strip()
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError as exc:
        raise ValueError(f"RUNTIME_MAX_ACTIVE_BOTS must be an integer, got {raw!r}") from exc
    if cap < 0:
        raise ValueError(f"RUNTIME_MAX_ACTIVE_BOTS must be 0 (no cap) or more, got {raw!r}")
    return cap or None


def _cap_recheck_s() -> float:
    """``RUNTIME_CAP_RECHECK_S``: how often (seconds) the cap reconciler checks the substrate for
    counted bots that exited (default 2). Anything that is not a number of seconds above 0 is fatal at
    boot."""
    from .kernel import DEFAULT_CAP_RECHECK_S

    raw = os.getenv("RUNTIME_CAP_RECHECK_S", "").strip()
    if not raw:
        return DEFAULT_CAP_RECHECK_S
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not value > 0 or value == float("inf"):
        raise ValueError(
            f"RUNTIME_CAP_RECHECK_S must be a number of seconds above 0, got {raw!r}"
        )
    return value


def _start_cap_reconciler(runtime, interval_s: float):
    """Start the runtime's one cap reconciler when a cluster cap is set: it marks counted bots that
    exited on the substrate ``stopped`` every ``interval_s``, so admission answers from memory and a
    slot frees within one interval of its bot's exit. Returns it, or None without a cap."""
    if not runtime.cluster_cap:
        return None
    from .kernel import CapReconciler

    return CapReconciler(runtime, interval_s=interval_s).start()


DEFAULT_FINISHED_POD_TTL_S = 300
FINISHED_SWEEP_INTERVAL_S = 60.0


def _finished_pod_ttl_s() -> int:
    """``RUNTIME_FINISHED_POD_TTL_S``: how long a finished bot Pod (Succeeded/Failed) is kept before
    the sweep deletes it (Kubernetes backend; default 300; 0 = no sweep). Anything that is not a
    whole number of seconds ≥ 0 is fatal at boot."""
    raw = os.getenv("RUNTIME_FINISHED_POD_TTL_S", "").strip()
    if not raw:
        return DEFAULT_FINISHED_POD_TTL_S
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise ValueError(f"RUNTIME_FINISHED_POD_TTL_S must be whole seconds, got {raw!r}") from exc
    if ttl < 0:
        raise ValueError(f"RUNTIME_FINISHED_POD_TTL_S must be 0 (off) or more, got {raw!r}")
    return ttl


def _start_finished_sweep(runtime, backend, ttl_s: int,
                          interval_s: float = FINISHED_SWEEP_INTERVAL_S):
    """Run ``runtime.sweep_finished(ttl_s)`` every ``interval_s`` in a daemon thread — Kubernetes
    only (Docker and process workloads are not swept) and only with a TTL above 0. Returns the
    thread, or None when no sweep runs."""
    if not ttl_s or getattr(backend, "name", None) != "k8s":
        return None

    def _loop() -> None:
        while True:
            try:
                runtime.sweep_finished(ttl_s)
            except Exception as e:  # noqa: BLE001 — a failed sweep must not kill the loop
                logger.warning("finished-pod sweep error: %s", e)
            time.sleep(interval_s)

    thread = threading.Thread(target=_loop, name="finished-pod-sweep", daemon=True)
    thread.start()
    return thread


def build_production_app():
    """Wire the runtime API with the env-selected spawn backend + the env-driven profile registry,
    plus the durable cron scheduler (REDIS_URL) with a background tick loop."""
    from .api import create_app
    from .config_preflight import preflight
    from .kernel import Runtime
    from .profiles import apply_command_overrides, default_registry, worker_image_for

    # config.v1 boot preflight (ADR-0026): validate the declaration against the env — the runtime has
    # no required-explicit keys today, so this logs the capability tri-states (scheduler · bot_spawn ·
    # agent_spawn · model_inference, incl. the credentials-file probe that catches a SET
    # HOST_CLAUDE_CREDENTIALS whose host file is absent) so a deploy's config completeness is visible
    # in the boot log and on /health BEFORE any workload runs. Capabilities never block boot.
    preflight()

    cluster_cap = _cluster_cap()
    cap_recheck_s = _cap_recheck_s()
    finished_pod_ttl_s = _finished_pod_ttl_s()
    backend = _build_backend()
    # The agent worker is its OWN image (core/agent/worker/Dockerfile — claude-code + node + the
    # `worker` package), NOT a rename of the agent-api image. With the Docker backend we ensure that
    # image is present up front — pulling it when absent, since the socket create API never
    # implicit-pulls and the compose agent-worker service is a build-only profile `up` skips. On
    # failure the worker name is still pinned so a dispatch fails loudly with 'No such image' rather
    # than silently spawning agent-api bytes that die with 'No module named worker'. Other backends
    # (k8s/process) pull by full ref themselves, so we just derive AGENT_WORKER_IMAGE.
    agent_image = os.getenv("AGENT_IMAGE", "")
    if agent_image:
        target = worker_image_for(agent_image)
        if hasattr(backend, "ensure_worker_image"):
            try:
                target = backend.ensure_worker_image(target)
            except Exception as e:  # noqa: BLE001 — startup image ensure must never crash the boot
                logger.warning("worker image ensure failed: %s; keeping %s", e, target)
        os.environ["AGENT_WORKER_IMAGE"] = target

    scheduler = _build_scheduler()
    if scheduler is not None:
        _start_ticker(scheduler)
    # apply_command_overrides is a no-op unless BOT_COMMAND / AGENT_WORKER_COMMAND are set (the
    # process-backend / `lite` case) — docker/k8s keep the image entrypoints unchanged.
    profiles = apply_command_overrides(default_registry())
    runtime = Runtime(
        backend=backend, profiles=profiles, grace_sec=_kernel_grace_sec(),
        cluster_cap=cluster_cap, cap_recheck_s=cap_recheck_s,
    )
    # Re-adopt the workloads this runtime spawned that are STILL on the substrate (containers/pods
    # survive a runtime recreate untouched): without this, the fresh in-memory registry 404s over a
    # live bot, and the control plane misreads that 404 as "bot gone" — the orphaned-live-bot
    # incident. Runs BEFORE uvicorn serves, so the first GET /workloads answer is already truthful.
    try:
        adopted = runtime.adopt()
        if adopted:
            logger.info(
                "re-adopted %d workload(s) found on the %s substrate after restart",
                adopted, backend.name,
            )
    except Exception as e:  # noqa: BLE001 — adoption is a boot aid; it must never block the boot
        logger.warning("workload re-adoption failed: %s", e)
    _start_finished_sweep(runtime, backend, finished_pod_ttl_s)
    _start_cap_reconciler(runtime, cap_recheck_s)
    return create_app(runtime, scheduler=scheduler)


def main() -> None:
    import uvicorn

    uvicorn.run(
        build_production_app(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8090")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
