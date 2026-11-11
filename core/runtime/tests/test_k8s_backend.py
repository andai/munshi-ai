"""The k8s backend. Offline: the spawned Pod's `/dev/shm` (a memory-backed emptyDir from
RUNTIME_K8S_SHM_SIZE, default 2Gi) is on every Pod, alone or merged with the other overlay seams,
and an invalid size is fatal at boot. Live: create a Pod via the kernel against a REAL cluster,
drive it through the runtime.v1 lifecycle, and assert it genuinely scheduled, ran, and was removed —
skipped where no cluster is reachable (e.g. laptop/CI without a KUBECONFIG)."""
import json
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


def _k8s_ok() -> bool:
    if not shutil.which("kubectl"):
        return False
    return subprocess.run(["kubectl", "get", "nodes"], capture_output=True).returncode == 0


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


@pytest.mark.skipif(not _k8s_ok(), reason="no reachable kubernetes cluster")
def test_k8s_backend_real_pod_lifecycle():
    name = "munshi-rt-k8stest"
    subprocess.run(["kubectl", "delete", "pod", name, "--ignore-not-found",
                    "--grace-period=0", "--force"], capture_output=True)  # clean slate
    rt = Runtime(
        backend=K8sBackend(),
        profiles={"test": Runnable(image="alpine", command=["sleep", "30"])},
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
