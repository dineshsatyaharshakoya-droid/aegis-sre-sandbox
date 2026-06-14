"""Tests for the logs read tool (C3), driving the real query() via a mocked
httpx transport (so the actual parsing/error code is exercised)."""

import asyncio

import httpx
import pytest

from aegis_sre.orchestrator import logs_tools
from aegis_sre.orchestrator.logs_tools import LogClient, LogQueryError, format_lines


@pytest.fixture
def mock_http(monkeypatch):
    """Route logs_tools' httpx.AsyncClient through a MockTransport with `handler`."""
    def install(handler):
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        def factory(*a, **k):
            return real(transport=transport)

        monkeypatch.setattr(logs_tools.httpx, "AsyncClient", factory)
    return install


def _streams(*lines):
    return {"status": "success", "data": {"resultType": "streams", "result": [
        {"stream": {"app": "api"}, "values": [[str(int(t * 1e9)), msg] for t, msg in lines]}]}}


def test_query_parses_and_sorts_lines(mock_http):
    mock_http(lambda req: httpx.Response(200, json=_streams((2.0, "second"), (1.0, "first"))))
    lines = asyncio.run(LogClient("http://loki.test").query('{app="api"}', limit=10))
    assert [l.line for l in lines] == ["first", "second"]
    assert lines[0].stream["app"] == "api"


def test_query_status_error_raises(mock_http):
    mock_http(lambda req: httpx.Response(400, json={"status": "error", "error": "parse error"}))
    with pytest.raises(LogQueryError, match="parse error"):
        asyncio.run(LogClient("http://loki.test").query("{bad"))


def test_query_non_json_raises(mock_http):
    mock_http(lambda req: httpx.Response(200, text="<html>oops"))
    with pytest.raises(LogQueryError, match="non-JSON"):
        asyncio.run(LogClient("http://loki.test").query("{}"))


def test_query_transport_error_raises():
    c = LogClient("http://127.0.0.1:1/")
    c.timeout = 0.5
    with pytest.raises(LogQueryError):
        asyncio.run(c.query("{}"))


def test_format_lines_block(mock_http):
    mock_http(lambda req: httpx.Response(200, json=_streams((1.0, "boom error"))))
    lines = asyncio.run(LogClient("http://loki.test").query("{}"))
    assert "boom error" in format_lines("{}", lines)
    assert format_lines("{}", []) == "`{}` -> (no log lines)"


def test_requires_base_url():
    with pytest.raises(ValueError):
        LogClient("")


def test_get_logs_client_none_when_unset(monkeypatch):
    logs_tools._client = None
    monkeypatch.setattr(logs_tools, "get_settings", lambda: type("S", (), {"logs_url": ""})())
    assert logs_tools.get_logs_client() is None


def test_get_logs_client_builds_when_set(monkeypatch):
    logs_tools._client = None
    monkeypatch.setattr(logs_tools, "get_settings",
                        lambda: type("S", (), {"logs_url": "http://loki:3100"})())
    c = logs_tools.get_logs_client()
    assert isinstance(c, LogClient) and c.base_url == "http://loki:3100"
    logs_tools._client = None
