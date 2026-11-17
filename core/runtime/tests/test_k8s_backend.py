"""The k8s backend. Offline: the spawned Pod's `/dev/shm` (a memory-backed emptyDir from
RUNTIME_K8S_SHM_SIZE, default 2Gi) is on every Pod, alone or merged with the other overlay seams,
and an invalid size is fatal at boot. Live: create a Pod via the kernel against a REAL cluster,
drive it through the runtime.v1 lifecycle, and assert it genuinely scheduled, ran, and was removed.
The live test runs only on the cluster named by RUNTIME_K8S_TEST_CONTEXT (a kubeconfig context) and
is skipped without it; the ambient kubectl context is never used, so a developer's shared cluster is
never touched by accident."""
import json
import os
import shutil
import subprocess
import time

import pytest

from runtime_kernel import Runtime
from runtime_kernel.k8s_backend import K8sBackend, build_pod, pod_overrides
from runtime_kernel.models import Resources, RuntimeState, WorkloadSpec
from runtime_kernel.profiles import Runnable

SHM_VOLUME = {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}}
SHM_MOUNT = {"name": "dshm", "mountPath": "/dev/shm"}


TEST_CONTEXT_ENV = "RUNTIME_K8S_TEST_CONTEXT"


# ── offline: /dev/shm on every spawned Pod ────────────────────────────────────────────────────────

def test_empty_env_still_gets_the_default_shm():
    """A plain bot — no workspace store, no tolerations, no nodeSelector — gets its 2Gi shm. The
    overlay used to return None here, which left a bot with the kubelet's small default."""
    ov = pod_overrides({}, container_name="munshi-mtg-1")
    assert ov == {"spec": {
        "containers": [{"name": "munshi-mtg-1", "volumeMounts": [SHM_MOUNT]}],
        "volumes": [SHM_VOLUME],
    }}


def test_the_size_comes_from_the_env():
    ov = pod_overrides({"RUNTIME_K8S_SHM_SIZE": "1536Mi"}, container_name="c")
    assert ov["spec"]["volumes"] == [
        {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "1536Mi"}}
    ]


def test_an_empty_size_turns_shm_off():
    assert pod_overrides({"RUNTIME_K8S_SHM_SIZE": ""}, container_name="c") is None


def test_shm_merges_with_scheduling_and_workspace_mounts():
    """The other seams are emitted exactly as before; shm is appended after them."""
    tol = [{"key": "munshi.ai/pool", "operator": "Exists"}]
    env = {
        "MUNSHI_WORKSPACE_MOUNT_SOURCE": "munshi-agent-workspaces",
        "MUNSHI_WORKSPACE_MOUNT_TARGET": "/workspaces",
        "MUNSHI_WORKSPACE_PATH": "/workspaces/u1",
        "RUNTIME_K8S_TOLERATIONS": json.dumps(tol),
        "RUNTIME_K8S_NODE_SELECTOR": json.dumps({"pool": "bots"}),
    }
    spec = pod_overrides(env, container_name="w")["spec"]
    off = pod_overrides({**env, "RUNTIME_K8S_SHM_SIZE": ""}, container_name="w")["spec"]
    assert spec["volumes"] == [*off["volumes"], SHM_VOLUME]
    assert spec["containers"][0]["volumeMounts"] == [
        *off["containers"][0]["volumeMounts"], SHM_MOUNT,
    ]
    assert spec["tolerations"] == off["tolerations"] == tol
    assert spec["nodeSelector"] == off["nodeSelector"] == {"pool": "bots"}


def test_the_submitted_pod_mounts_shm_on_its_container_with_the_bot_limit():
    pod = build_pod(
        name="munshi-mtg-2", workload_id="mtg-2", runnable=Runnable(image="bot:test"),
        env={"BOT_CONFIG": "{}"}, namespace="munshi", resources=Resources(cpu=1, memoryMb=2560),
    )
    c = pod["spec"]["containers"][0]
    assert pod["spec"]["volumes"] == [SHM_VOLUME]
    assert c["volumeMounts"] == [SHM_MOUNT]
    assert c["image"] == "bot:test" and {"name": "BOT_CONFIG", "value": "{}"} in c["env"]
    assert c["resources"]["limits"]["memory"] == "2560Mi"


def test_the_runtime_process_env_carries_the_size_even_when_empty(monkeypatch):
    from runtime_kernel.k8s_backend import _runtime_scheduling_env

    monkeypatch.setenv("RUNTIME_K8S_SHM_SIZE", "")
    assert _runtime_scheduling_env()["RUNTIME_K8S_SHM_SIZE"] == ""
    monkeypatch.delenv("RUNTIME_K8S_SHM_SIZE")
    assert "RUNTIME_K8S_SHM_SIZE" not in _runtime_scheduling_env()


@pytest.mark.parametrize("bad", ["2 GB", "two", "-1Gi", "0", "2GiB", "1.5.0Gi"])
def test_an_invalid_size_is_fatal_at_boot(monkeypatch, bad):
    monkeypatch.setenv("RUNTIME_K8S_SHM_SIZE", bad)
    with pytest.raises(ValueError, match="RUNTIME_K8S_SHM_SIZE"):
        K8sBackend()


@pytest.mark.parametrize("good", ["2Gi", "1536Mi", "2G", "500M", "1.5Gi", "2147483648", ""])
def test_valid_sizes_boot(monkeypatch, good):
    monkeypatch.setenv("RUNTIME_K8S_SHM_SIZE", good)
    K8sBackend()


# ── live: a real Pod through the lifecycle ────────────────────────────────────────────────────────


def _pod_exists(name: str) -> bool:
    return subprocess.run(["kubectl", "get", "pod", name], capture_output=True).returncode == 0


def _phase(name: str) -> str:
    return subprocess.run(
        ["kubectl", "get", "pod", name, "-o", "jsonpath={.status.phase}"],
        capture_output=True, text=True,
    ).stdout.strip()


def _pin_test_context(tmp_path, monkeypatch) -> str:
    """Pin every kubectl call in this process (the test's and the backend's) to the opted-in
    context: a kubeconfig holding only that context, through KUBECONFIG."""
    context = os.environ.get(TEST_CONTEXT_ENV, "").strip()
    if not context:
        pytest.skip(f"set {TEST_CONTEXT_ENV} to a kubeconfig context for a test cluster")
    if not shutil.which("kubectl"):
        pytest.fail("kubectl is not installed")
    pinned = subprocess.run(
        ["kubectl", "config", "view", "--minify", "--flatten", "--context", context],
        capture_output=True, text=True,
    )
    if pinned.returncode != 0:
        pytest.fail(f"{TEST_CONTEXT_ENV}={context} is not a kubeconfig context")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(pinned.stdout)
    kubeconfig.chmod(0o600)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    if subprocess.run(["kubectl", "get", "nodes"], capture_output=True).returncode != 0:
        pytest.fail(f"the cluster of context {context} is not reachable")
    return context


@pytest.fixture
def opted_in_cluster(tmp_path, monkeypatch):
    return _pin_test_context(tmp_path, monkeypatch)


def test_without_an_opted_in_context_the_live_test_skips_without_calling_kubectl(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(TEST_CONTEXT_ENV, raising=False)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(pytest.skip.Exception):
        _pin_test_context(tmp_path, monkeypatch)
    assert calls == []


def test_k8s_backend_real_pod_lifecycle(opted_in_cluster):
    name = "munshi-rt-k8stest"
    subprocess.run(["kubectl", "delete", "pod", name, "--ignore-not-found",
                    "--grace-period=0", "--force"], capture_output=True)  # clean slate
    rt = Runtime(
        backend=K8sBackend(),
        # long enough to outlive a slow image pull; the test stops and destroys it
        profiles={"test": Runnable(image="alpine", command=["sleep", "600"])},
        grace_sec=30.0,
    )
    spec = WorkloadSpec(workloadId="rt-k8stest", profile="test", env={"MUNSHI_X": "y"})
    try:
        rt.create(spec)
        assert rt.get("rt-k8stest").state is RuntimeState.running
        assert _pod_exists(name)                              # a REAL pod object exists

        deadline = time.time() + 90                           # wait for it to actually schedule & run
        while time.time() < deadline and _phase(name) != "Running":
            time.sleep(1)
        assert _phase(name) == "Running"                      # genuinely scheduled & running

        rt.stop("rt-k8stest")
        assert rt.get("rt-k8stest").state is RuntimeState.stopped

        rt.destroy("rt-k8stest")
        assert rt.get("rt-k8stest").state is RuntimeState.destroyed
        assert not _pod_exists(name)                          # pod actually removed
    finally:
        subprocess.run(["kubectl", "delete", "pod", name, "--ignore-not-found",
                        "--grace-period=0", "--force"], capture_output=True)


# ── offline: every kubectl call is bounded (RUNTIME_KUBECTL_TIMEOUT_S, default 20) ──────────────────


def _record_runs(monkeypatch, outcome=None):
    """Replace subprocess.run inside the backend; return the kwargs of every call."""
    from runtime_kernel import k8s_backend

    calls: list[dict] = []

    def fake_run(argv, **kw):
        calls.append({"argv": argv, **kw})
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(argv, 0, stdout='{"status": {"phase": "Running"}}',
                                           stderr="")

    monkeypatch.setattr(k8s_backend.subprocess, "run", fake_run)
    return calls


def test_kubectl_runs_with_the_default_timeout(monkeypatch):
    from runtime_kernel.k8s_backend import _kubectl

    monkeypatch.delenv("RUNTIME_KUBECTL_TIMEOUT_S", raising=False)
    calls = _record_runs(monkeypatch)
    _kubectl("get", "pods")
    assert calls[0]["timeout"] == 20.0


def test_kubectl_timeout_comes_from_the_env(monkeypatch):
    from runtime_kernel.k8s_backend import _kubectl

    monkeypatch.setenv("RUNTIME_KUBECTL_TIMEOUT_S", "7.5")
    calls = _record_runs(monkeypatch)
    _kubectl("get", "pods", check=False)
    assert calls[0]["timeout"] == 7.5


@pytest.mark.parametrize("check", [True, False])
def test_a_kubectl_timeout_raises_a_backend_error_instead_of_hanging(monkeypatch, check):
    from runtime_kernel.k8s_backend import KubectlTimeout, _kubectl

    _record_runs(monkeypatch, subprocess.TimeoutExpired(["kubectl"], 20))
    with pytest.raises(KubectlTimeout, match="timed out") as err:
        _kubectl("get", "pods", check=check)
    assert isinstance(err.value, RuntimeError)


def test_an_exit_read_that_times_out_is_unknown_not_exited(monkeypatch):
    """A timed-out read must not mark a live bot stopped (and so free its slot or reclaim it)."""
    from runtime_kernel.backend import WorkloadHandle

    _record_runs(monkeypatch, subprocess.TimeoutExpired(["kubectl"], 20))
    assert K8sBackend().exit_code(WorkloadHandle(id="w", impl="munshi-w")) is None


@pytest.mark.parametrize("bad", ["0", "-3", "soon", "nan"])
def test_an_invalid_kubectl_timeout_is_fatal_at_boot(monkeypatch, bad):
    monkeypatch.setenv("RUNTIME_KUBECTL_TIMEOUT_S", bad)
    with pytest.raises(ValueError, match="RUNTIME_KUBECTL_TIMEOUT_S"):
        K8sBackend()
