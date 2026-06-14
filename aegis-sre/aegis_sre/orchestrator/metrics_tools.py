"""
Prometheus query tool — the agent's observability eyes (roadmap C2).

This is the *read* side of metrics, distinct from `telemetry/metrics.py` (which
*exports* Aegis's own counters). Here the agent runs raw PromQL against a
Prometheus HTTP API and parses the JSON so the researcher node can fold live
signal (error rates, saturation, restarts) into its diagnosis instead of
reasoning from the stack trace alone.

Public surface:
  * `PrometheusClient.query(promql)`        -> instant query  -> list[MetricSample]
  * `PrometheusClient.query_range(...)`     -> range query    -> list[MetricSeries]
  * `format_samples(...)` / `format_series` -> compact text block for an LLM prompt
  * `get_metrics_client()`                  -> client from settings (or None if unset)

Design notes:
  * httpx (already a dependency) for async HTTP; a hard per-call timeout so a slow
    Prometheus can never stall the repair graph.
  * Prometheus signals query errors two ways: a non-2xx body, or HTTP 200 with
    `{"status":"error"}`. Both are normalized to `PrometheusQueryError`.
  * The client never raises into the graph: `researcher_node` calls it guarded,
    so an observability outage degrades to "no live metrics", never a failed repair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from aegis_sre.config import get_settings
from aegis_sre.telemetry.logger import logger


class PrometheusQueryError(RuntimeError):
    """A PromQL query failed (transport error, non-2xx, or status=error)."""


@dataclass
class MetricSample:
    """One instant value: a metric's labels + (timestamp, value)."""

    metric: dict
    timestamp: float
    value: float

    @property
    def name(self) -> str:
        return self.metric.get("__name__", "")


@dataclass
class MetricSeries:
    """One labeled series of (timestamp, value) points (range query)."""

    metric: dict
    values: list[tuple[float, float]]

    @property
    def name(self) -> str:
        return self.metric.get("__name__", "")


def _to_float(raw: str) -> float:
    # Prometheus encodes values as strings, incl. "NaN"/"+Inf"/"-Inf".
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float("nan")


class PrometheusClient:
    """Thin async client over the Prometheus HTTP API (`/api/v1`)."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        if not base_url:
            raise ValueError("PrometheusClient requires a base_url")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}/api/v1/{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            raise PrometheusQueryError(f"Prometheus request failed: {e}") from e

        # Prometheus returns a JSON envelope even on 4xx (with status=error).
        try:
            body = resp.json()
        except ValueError as e:
            raise PrometheusQueryError(
                f"Prometheus returned non-JSON (HTTP {resp.status_code})"
            ) from e

        if body.get("status") != "success":
            raise PrometheusQueryError(
                f"PromQL error: {body.get('errorType', 'error')}: {body.get('error', body)}"
            )
        return body.get("data", {})

    async def query(self, promql: str, time: Optional[float] = None) -> list[MetricSample]:
        """Run an instant PromQL query. Returns one `MetricSample` per series
        (scalar/string results are wrapped as a single sample)."""
        params: dict = {"query": promql}
        if time is not None:
            params["time"] = time
        data = await self._get("query", params)
        rtype, result = data.get("resultType"), data.get("result", [])

        samples: list[MetricSample] = []
        if rtype == "vector":
            for item in result:
                ts, val = item["value"]
                samples.append(MetricSample(metric=item.get("metric", {}), timestamp=float(ts), value=_to_float(val)))
        elif rtype in ("scalar", "string"):
            ts, val = result
            samples.append(MetricSample(metric={}, timestamp=float(ts), value=_to_float(val)))
        elif rtype == "matrix":
            # An instant query shouldn't return a matrix, but tolerate it: take
            # the last point of each series.
            for item in result:
                pts = item.get("values", [])
                if pts:
                    ts, val = pts[-1]
                    samples.append(MetricSample(metric=item.get("metric", {}), timestamp=float(ts), value=_to_float(val)))
        logger.info("prometheus_query", promql=promql, series=len(samples))
        return samples

    async def query_range(
        self, promql: str, start: float, end: float, step: str = "60s"
    ) -> list[MetricSeries]:
        """Run a range PromQL query over `[start, end]` at `step` resolution."""
        data = await self._get(
            "query_range", {"query": promql, "start": start, "end": end, "step": step}
        )
        series: list[MetricSeries] = []
        for item in data.get("result", []):
            pts = [(float(ts), _to_float(v)) for ts, v in item.get("values", [])]
            series.append(MetricSeries(metric=item.get("metric", {}), values=pts))
        logger.info("prometheus_query_range", promql=promql, series=len(series))
        return series


# --- formatting + factory ----------------------------------------------------


def _label_str(metric: dict) -> str:
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    name = metric.get("__name__", "")
    return f"{name}{{{inner}}}" if inner else (name or "{}")


def format_samples(promql: str, samples: list[MetricSample], limit: int = 10) -> str:
    """Render instant-query results as a compact text block for an LLM prompt."""
    if not samples:
        return f"`{promql}` -> (no data)"
    lines = [f"`{promql}` ->"]
    for s in samples[:limit]:
        lines.append(f"  {_label_str(s.metric)} = {s.value:g}")
    if len(samples) > limit:
        lines.append(f"  … (+{len(samples) - limit} more series)")
    return "\n".join(lines)


_client: Optional[PrometheusClient] = None


def get_metrics_client() -> Optional[PrometheusClient]:
    """Process-wide client built from settings, or None when PROMETHEUS_URL is
    unset (callers treat None as 'observability disabled')."""
    global _client
    if _client is None:
        url = get_settings().prometheus_url
        if not url:
            return None
        _client = PrometheusClient(url)
    return _client
