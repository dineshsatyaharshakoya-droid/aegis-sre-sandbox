import os
import time
import hashlib
from collections import OrderedDict
from kubernetes import client, config, watch
from aegis_sre.orchestrator.schemas import TelemetryEvent
from aegis_sre.telemetry.cache import IdempotencyCache

class K8sTelemetryWatcher:
    def __init__(self, namespace="default", deduplication_ttl=300):
        self.namespace = namespace
        self.idempotency_cache = IdempotencyCache(ttl_seconds=deduplication_ttl)
        try:
            # Try to load in-cluster config if running inside k8s, otherwise local kubeconfig
            config.load_incluster_config()
        except config.ConfigException:
            try:
                config.load_kube_config()
            except config.ConfigException:
                print("[Telemetry] WARNING: Could not configure kubernetes client. Mocking telemetry.")
                self.v1 = None
                return
                
        self.v1 = client.CoreV1Api()

    def watch_for_crashes(self):
        """
        Watches the target namespace for pods entering CrashLoopBackOff or Error states.
        Yields TelemetryEvent objects.
        """
        if not self.v1:
            # Yield a mock event for testing if K8s is not available
            print("[Telemetry] Yielding mock crash event for testing...")
            yield TelemetryEvent(
                event_id="mock-k8s-001",
                service_name="payment-service",
                crash_log="Traceback (most recent call last):\n  File 'app.py', line 10, in process\n    1/0\nZeroDivisionError: division by zero",
                metadata={"namespace": self.namespace, "mock": True}
            )
            return

        print(f"[Telemetry] Watching namespace '{self.namespace}' for pod crashes...")
        w = watch.Watch()
        
        for event in w.stream(self.v1.list_namespaced_pod, self.namespace):
            pod = event['object']
            
            # Check if pod has container statuses
            if not pod.status.container_statuses:
                continue
                
            for status in pod.status.container_statuses:
                state = status.state
                
                # Check for crashed states. Parenthesized explicitly so the
                # intent does not depend on Python's and/or precedence.
                is_crashloop = bool(state.waiting and state.waiting.reason == 'CrashLoopBackOff')
                is_errored = bool(state.terminated and state.terminated.reason == 'Error')
                if is_crashloop or is_errored:
                       
                    print(f"[Telemetry] Detected crash in pod {pod.metadata.name}")
                    
                    # Fetch logs of the crashed container
                    try:
                        logs = self.v1.read_namespaced_pod_log(
                            name=pod.metadata.name,
                            namespace=self.namespace,
                            container=status.name,
                            tail_lines=50,
                            previous=True # Get logs from the crashed instance, not the restarting one
                        )
                    except Exception as e:
                        logs = f"Could not retrieve logs: {e}"
                        
                    # Idempotency Check: Hash the pod name and the tail of the logs
                    signature = f"{pod.metadata.name}:{logs[-200:]}".encode('utf-8')
                    crash_hash = hashlib.sha256(signature).hexdigest()

                    if self.idempotency_cache.is_duplicate(crash_hash):
                        print(f"[Telemetry] Dropping duplicate crash event for {pod.metadata.name} (Hash: {crash_hash[:8]})")
                        continue
                        
                    telemetry = TelemetryEvent(
                        event_id=f"{pod.metadata.name}-{int(time.time())}",
                        service_name=pod.metadata.labels.get('app', pod.metadata.name) if pod.metadata.labels else pod.metadata.name,
                        crash_log=logs,
                        metadata={
                            "pod_name": pod.metadata.name,
                            "namespace": self.namespace,
                            "container": status.name,
                            "reason": state.waiting.reason if state.waiting else state.terminated.reason
                        }
                    )
                    
                    yield telemetry
