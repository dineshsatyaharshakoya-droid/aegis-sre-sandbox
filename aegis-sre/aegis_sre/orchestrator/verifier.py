"""
Post-action verification (Stone 3, D4) — proof a remediation actually worked.

After a live action runs, autonomy is only safe if we can confirm the triggering
signal cleared (and, in D5, roll back if it didn't). The Verifier re-reads the
relevant metric via the Prometheus read tool and checks it now satisfies a
"healthy" condition.

The check carries an explicit PromQL + comparator + threshold; the query should
return a single value (use PromQL aggregation). If it yields multiple series we
take the worst case (max) so verification is conservative — a fix only counts as
verified when *every* series is healthy.

Fails closed: no client, no data, or a query error all return verified=False —
we never claim recovery we couldn't observe.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from aegis_sre.telemetry.logger import logger


class Comparator(str, Enum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"


@dataclass
class VerificationCheck:
    query: str
    comparator: Comparator
    threshold: float


@dataclass
class VerificationResult:
    verified: bool
    observed: Optional[float]
    query: str
    detail: str


_OPS = {
    Comparator.LT: lambda v, t: v < t,
    Comparator.LTE: lambda v, t: v <= t,
    Comparator.GT: lambda v, t: v > t,
    Comparator.GTE: lambda v, t: v >= t,
    Comparator.EQ: lambda v, t: v == t,
}


class Verifier:
    """Re-reads a metric and checks it satisfies a healthy condition."""

    def __init__(self, client=None):
        self._client = client

    def _resolve_client(self):
        if self._client is not None:
            return self._client
        from aegis_sre.orchestrator.metrics_tools import get_metrics_client
        return get_metrics_client()

    async def verify(self, check: VerificationCheck) -> VerificationResult:
        client = self._resolve_client()
        if client is None:
            return VerificationResult(False, None, check.query, "no prometheus client configured")
        try:
            samples = await client.query(check.query)
        except Exception as e:  # noqa: BLE001 - fail closed, never claim unobserved recovery
            logger.warning("verification_query_failed", query=check.query, error=str(e))
            return VerificationResult(False, None, check.query, f"query error: {e}")

        if not samples:
            return VerificationResult(False, None, check.query, "no data returned")

        # Conservative: EVERY series must satisfy the condition. The worst-case
        # value to report depends on the comparator's direction — for "should be
        # low" (LT/LTE) that's the max; for "should be high" (GT/GTE) it's the min.
        values = [s.value for s in samples]
        op = _OPS[check.comparator]
        ok = all(op(v, check.threshold) for v in values)
        if check.comparator in (Comparator.LT, Comparator.LTE):
            observed = max(values)
        elif check.comparator in (Comparator.GT, Comparator.GTE):
            observed = min(values)
        else:  # EQ: report a violating value if any, else the threshold
            observed = next((v for v in values if v != check.threshold), check.threshold)
        detail = f"observed {observed:g} {check.comparator.value} {check.threshold:g} -> {ok}"
        logger.info("verification_evaluated", query=check.query, observed=observed,
                    comparator=check.comparator.value, threshold=check.threshold, verified=ok)
        return VerificationResult(ok, observed, check.query, detail)
