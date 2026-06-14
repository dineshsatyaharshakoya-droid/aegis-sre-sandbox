"""
Kubernetes act-tools (audit #7) — real `kubectl`-backed remediation handlers.

These are the ACT tools an ActionPlan can invoke (cordon/drain a node, scale or
restart a deployment, delete a crashlooping pod). They shell out to `kubectl`, so
they act against whatever cluster the worker's kubeconfig points at. With no
cluster configured they raise a clear error — which is correct: the executor
dry-runs by default and the policy gates live execution, so an unconfigured
cluster simply means the action never runs live.
"""

from __future__ import annotations

import asyncio

from aegis_sre.telemetry.logger import logger


async def _kubectl(*args: str) -> str:
    """Run `kubectl <args>` and return stdout; raise with stderr on failure."""
    proc = await asyncio.create_subprocess_exec(
        "kubectl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {err.decode('utf-8', 'replace').strip()}")
    logger.info("kubectl_ok", argv=" ".join(args))
    return out.decode("utf-8", "replace")


async def cordon_node(node: str, **_) -> str:
    return await _kubectl("cordon", node)


async def uncordon_node(node: str, **_) -> str:
    return await _kubectl("uncordon", node)


async def drain_node(node: str, **_) -> str:
    return await _kubectl("drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--force")


async def scale_deployment(deployment: str, replicas: int, namespace: str = "default", **_) -> str:
    return await _kubectl("scale", f"deployment/{deployment}", f"--replicas={replicas}", "-n", namespace)


async def restart_deployment(deployment: str, namespace: str = "default", **_) -> str:
    return await _kubectl("rollout", "restart", f"deployment/{deployment}", "-n", namespace)


async def delete_pod(pod: str, namespace: str = "default", **_) -> str:
    return await _kubectl("delete", "pod", pod, "-n", namespace)


# name -> handler, registered as ACT tools in the registry.
K8S_ACT_TOOLS = {
    "k8s.cordon_node": cordon_node,
    "k8s.uncordon_node": uncordon_node,
    "k8s.drain_node": drain_node,
    "k8s.scale_deployment": scale_deployment,
    "k8s.restart_deployment": restart_deployment,
    "k8s.delete_pod": delete_pod,
}
