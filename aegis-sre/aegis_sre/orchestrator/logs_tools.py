"""
Logs query tool (Stone 2, C3) — another of the agent's read-only "eyes".

Queries a Loki-compatible logs HTTP API with LogQL so the researcher can fold
recent log lines for the affected service into its diagnosis, alongside metrics
(C2) and VCS/RAG context. Read-risk only; like the metrics tool it never mutates
anything and fails closed (returns nothing rather than raising into the graph).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import httpx

from aegis_sre.config import get_settings
from aegis_sre.telemetry.logger import logger


class LogQueryError(RuntimeError):
    """A LogQL query failed (transport error, non-2xx, or status=error)."""


@dataclass
class LogLine:
    stream: dict
    timestamp: float  # seconds
    line: str


class LogClient:
    """Thin async client over a Loki-compatible HTTP API (`/loki/api/v1`)."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        if not base_url:
            raise ValueError("LogClient requires a base_url")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def query(self, logql: str, limit: int = 100,
                    start: Optional[float] = None, end: Optional[float] = None) -> List[LogLine]:
        params: dict = {"query": logql, "limit": limit}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        url = f"{self.base_url}/loki/api/v1/query_range"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            raise LogQueryError(f"Loki request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise LogQueryError(f"Loki returned non-JSON (HTTP {resp.status_code})") from e
        if body.get("status") != "success":
            raise LogQueryError(f"LogQL error: {body.get('error', body)}")

        lines: List[LogLine] = []
        for stream in body.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                # Loki timestamps are nanosecond strings.
                lines.append(LogLine(stream=labels, timestamp=float(ts) / 1e9, line=line))
        lines.sort(key=lambda l: l.timestamp)
        logger.info("logs_query", logql=logql, lines=len(lines))
        return lines


def format_lines(logql: str, lines: List[LogLine], limit: int = 20) -> str:
    if not lines:
        return f"`{logql}` -> (no log lines)"
    out = [f"`{logql}` ->"]
    for l in lines[-limit:]:
        out.append(f"  {l.line}")
    return "\n".join(out)


_client: Optional[LogClient] = None


def get_logs_client() -> Optional[LogClient]:
    """Process-wide client from settings, or None when LOKI_URL is unset."""
    global _client
    if _client is None:
        url = get_settings().logs_url
        if not url:
            return None
        _client = LogClient(url)
    return _client
