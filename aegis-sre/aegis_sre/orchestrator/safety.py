import os
from typing import Tuple

class SafetyPolicy:
    """
    Unified Safety Policy for the Aegis SRE AI Swarm.
    Replaces disjointed graph retries and hardcoded queue timeouts.
    """
    def __init__(self):
        # Allow environment variable overrides for Enterprise environments
        self.max_retries = int(os.environ.get("AEGIS_MAX_RETRIES", 3))
        self.global_timeout_seconds = int(os.environ.get("AEGIS_GLOBAL_TIMEOUT", 120))
        
    def get_timeout(self) -> int:
        return self.global_timeout_seconds
        
    def should_abort(self, state: dict) -> Tuple[bool, str]:
        """
        Check if the current graph execution should be aborted.
        Returns (should_abort, reason)
        """
        iterations = state.get("iteration_count", 0)
        
        if iterations >= self.max_retries:
            return True, f"Max retries ({self.max_retries}) exceeded. Possible hallucination loop."
            
        return False, ""

# Global singleton policy
safety_policy = SafetyPolicy()
