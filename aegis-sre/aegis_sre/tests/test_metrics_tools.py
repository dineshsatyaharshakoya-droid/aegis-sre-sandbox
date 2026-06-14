"""
Tests for the Prometheus query tool (`orchestrator/metrics_tools.py`).

No live Prometheus needed: an `httpx.MockTransport` returns canned API envelopes,
so vector/scalar/matrix parsing and both error paths (non-2xx and status=error)
are exercised deterministically.
"""

import asyncio

import httpx
import pytest

from aegis_sre.orchestrator import metrics_tools
from aegis_sre.orchestrator.metrics_tools import (
    PrometheusClient,
    PrometheusQueryError,
    format_samples,
)


def _client_with(handler) -> PrometheusClient:
    """A PrometheusClient whose HTTP calls are served by `handler`."""
    transport = httpx.MockTransport(handler)
    c = PrometheusClient("http://prom.test")

    async def _get(path, params):
        url = f"{c.base_url}/api/v1/{path}"
        async with httpx.AsyncClient(transport=transport) as ac:
            resp = await ac.get(url, params=params)
        body = resp.json()
        if body.get("status") != "success":
            raise PrometheusQueryError(body.get("error", "error"))
        return body.get("data", {})

    c._get = _get  # type: ignore[method-assign]
    return c


def _vector(*pairs):
    return {
        "status": "success",
        "data": {"resultType": "vector",
                 "result": [{"metric": m, "value": [1.0, v]} for m, v in pairs]},
    }


def test_query_parses_vector():
    def handler(request):
        return httpx.Response(200, json=_vector(({"__name__": "up", "job": "api"}, "1"),
                                                ({"__name__": "up", "job": "worker"}, "0")))
    c = _client_with(handler)
    samples = asyncio.run(c.query("up"))
    assert len(samples) == 2
    assert samples[0].name == "up"
    assert samples[0].metric["job"] == "api"
    assert samples[0].value == 1.0
    assert samples[1].value == 0.0


def test_query_parses_scalar():
    def handler(request):
        return httpx.Response(200, json={"status": "success",
                                         "data": {"resultType": "scalar", "result": [1.0, "42"]}})
    c = _client_with(handler)
    samples = asyncio.run(c.query("vector(42)"))
    assert len(samples) == 1
    assert samples[0].value == 42.0


def test_query_handles_nan():
    def handler(request):
        return httpx.Response(200, json=_vector(({"__name__": "x"}, "NaN")))
    c = _client_with(handler)
    samples = asyncio.run(c.query("x"))
    assert samples[0].value != samples[0].value  # NaN


def test_query_range_parses_matrix():
    def handler(request):
        return httpx.Response(200, json={
            "status": "success",
            "data": {"resultType": "matrix",
                     "result": [{"metric": {"__name__": "rate"},
                                 "values": [[1.0, "0.1"], [2.0, "0.2"]]}]},
        })
    c = _client_with(handler)
    series = asyncio.run(c.query_range("rate(x[5m])", start=1.0, end=2.0, step="1s"))
    assert len(series) == 1
    assert series[0].values == [(1.0, 0.1), (2.0, 0.2)]


def test_status_error_raises():
    def handler(request):
        return httpx.Response(400, json={"status": "error", "errorType": "bad_data",
                                         "error": "parse error: unexpected )"})
    c = _client_with(handler)
    with pytest.raises(PrometheusQueryError, match="parse error"):
        asyncio.run(c.query("up)"))


def test_transport_error_raises():
    """A real connection failure (no MockTransport) surfaces as PrometheusQueryError."""
    c = PrometheusClient("http://127.0.0.1:1/")  # unroutable port
    c.timeout = 0.5
    with pytest.raises(PrometheusQueryError):
        asyncio.run(c.query("up"))


def test_format_samples_compact_block():
    def handler(request):
        return httpx.Response(200, json=_vector(({"__name__": "up", "job": "api"}, "1")))
    c = _client_with(handler)
    samples = asyncio.run(c.query("up"))
    out = format_samples("up", samples)
    assert "up{job=\"api\"} = 1" in out
    assert format_samples("up", []) == "`up` -> (no data)"


def test_get_metrics_client_none_when_unset(monkeypatch):
    metrics_tools._client = None
    monkeypatch.setattr(metrics_tools, "get_settings",
                        lambda: type("S", (), {"prometheus_url": ""})())
    assert metrics_tools.get_metrics_client() is None
