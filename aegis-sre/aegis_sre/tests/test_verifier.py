"""
Tests for post-action verification (D4).

A fake metrics client returns canned samples so the comparator logic, the
conservative worst-case-across-series rule, and the fail-closed paths (no client,
no data, query error) are all deterministic.
"""

import asyncio

from aegis_sre.orchestrator.metrics_tools import MetricSample
from aegis_sre.orchestrator.verifier import Comparator, VerificationCheck, Verifier


class _FakeClient:
    def __init__(self, values=None, exc=None):
        self._values = values
        self._exc = exc

    async def query(self, promql, **kwargs):
        if self._exc:
            raise self._exc
        return [MetricSample(metric={"__name__": "x"}, timestamp=1.0, value=v) for v in (self._values or [])]


def _check(comp=Comparator.LT, threshold=0.05):
    return VerificationCheck(query="rate(http_5xx[5m])", comparator=comp, threshold=threshold)


def test_verified_when_metric_healthy():
    v = Verifier(client=_FakeClient(values=[0.01]))
    r = asyncio.run(v.verify(_check(Comparator.LT, 0.05)))
    assert r.verified is True and r.observed == 0.01


def test_not_verified_when_metric_still_bad():
    v = Verifier(client=_FakeClient(values=[0.10]))
    r = asyncio.run(v.verify(_check(Comparator.LT, 0.05)))
    assert r.verified is False and r.observed == 0.10


def test_worst_case_across_series_is_conservative():
    # one bad series among good ones must fail verification (max = 0.2)
    v = Verifier(client=_FakeClient(values=[0.01, 0.2, 0.0]))
    r = asyncio.run(v.verify(_check(Comparator.LT, 0.05)))
    assert r.observed == 0.2 and r.verified is False


def test_gte_comparator_for_liveness():
    # e.g. up >= 1 means recovered
    v = Verifier(client=_FakeClient(values=[1.0]))
    assert asyncio.run(v.verify(_check(Comparator.GTE, 1.0))).verified is True
    v2 = Verifier(client=_FakeClient(values=[1.0, 0.0]))  # one instance down
    assert asyncio.run(v2.verify(_check(Comparator.GTE, 1.0))).verified is False


def test_no_data_fails_closed():
    r = asyncio.run(Verifier(client=_FakeClient(values=[])).verify(_check()))
    assert r.verified is False and "no data" in r.detail


def test_query_error_fails_closed():
    r = asyncio.run(Verifier(client=_FakeClient(exc=RuntimeError("boom"))).verify(_check()))
    assert r.verified is False and "query error" in r.detail


def test_no_client_fails_closed():
    # Force the no-client path deterministically (don't depend on PROMETHEUS_URL).
    v = Verifier()
    v._resolve_client = lambda: None
    r = asyncio.run(v.verify(_check()))
    assert r.verified is False and "no prometheus client" in r.detail
