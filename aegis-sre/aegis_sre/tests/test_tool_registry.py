"""
Tests for the risk-classed tool registry (C1).

The acceptance is "registry lists read/act tools" and — crucially for the
Stone-3 policy — that gating derives correctly from the risk class.
"""

import pytest

from aegis_sre.integrations.tool_registry import (
    RiskClass,
    ToolRegistry,
    build_default_registry,
    get_tool_registry,
)


def test_register_and_get():
    reg = ToolRegistry()
    t = reg.register("k8s.get_pods", RiskClass.READ, "list pods")
    assert reg.get("k8s.get_pods") is t
    assert t.risk is RiskClass.READ


def test_duplicate_registration_rejected():
    reg = ToolRegistry()
    reg.register("x", RiskClass.READ, "d")
    with pytest.raises(ValueError, match="already registered"):
        reg.register("x", RiskClass.ACT, "d2")


def test_unknown_tool_raises():
    with pytest.raises(KeyError, match="no such tool"):
        ToolRegistry().get("nope")


def test_bad_risk_rejected():
    with pytest.raises(ValueError, match="must be a RiskClass"):
        ToolRegistry().register("x", "read", "d")  # str, not RiskClass


def test_requires_approval_only_for_act():
    reg = ToolRegistry()
    reg.register("r", RiskClass.READ, "d")
    reg.register("n", RiskClass.NOTIFY, "d")
    reg.register("a", RiskClass.ACT, "d")
    assert reg.requires_approval("a") is True
    assert reg.requires_approval("r") is False
    assert reg.requires_approval("n") is False


def test_list_filters_by_risk_and_sorts():
    reg = ToolRegistry()
    reg.register("b.act", RiskClass.ACT, "d")
    reg.register("a.read", RiskClass.READ, "d")
    reg.register("c.read", RiskClass.READ, "d")
    reads = reg.list(risk=RiskClass.READ)
    assert [t.name for t in reads] == ["a.read", "c.read"]
    assert {t.name for t in reg.list()} == {"a.read", "b.act", "c.read"}


def test_default_registry_classifies_known_tools():
    reg = build_default_registry()
    by_name = {t.name: t for t in reg.list()}
    assert by_name["prometheus.query"].risk is RiskClass.READ
    assert by_name["prometheus.query_range"].risk is RiskClass.READ
    assert by_name["incident.trigger"].risk is RiskClass.NOTIFY
    assert by_name["incident.resolve"].risk is RiskClass.NOTIFY
    assert by_name["gitops.create_pull_request"].risk is RiskClass.ACT


def test_default_registry_gated_tools_are_exactly_the_act_tools():
    reg = build_default_registry()
    gated = {t.name for t in reg.gated_tools()}
    assert gated == {"gitops.create_pull_request"}  # the only thing that mutates managed state today


def test_read_tools_have_handlers_wired():
    reg = build_default_registry()
    assert callable(reg.get("prometheus.query").handler)
    assert callable(reg.get("gitops.create_pull_request").handler)


def test_get_tool_registry_is_singleton():
    assert get_tool_registry() is get_tool_registry()


# --- C6: metered invoke ---

def test_invoke_calls_handler_and_returns():
    import asyncio
    reg = ToolRegistry()
    seen = {}
    async def h(**kw):
        seen.update(kw); return "done"
    reg.register("k8s.scale", RiskClass.ACT, "scale", handler=h)
    out = asyncio.run(reg.invoke("k8s.scale", replicas=3))
    assert out == "done" and seen == {"replicas": 3}


def test_invoke_without_handler_raises():
    import asyncio
    reg = ToolRegistry()
    reg.register("noop", RiskClass.READ, "no handler")
    try:
        asyncio.run(reg.invoke("noop"))
        assert False
    except ValueError as e:
        assert "no handler" in str(e)


def test_invoke_records_error_and_reraises():
    import asyncio
    reg = ToolRegistry()
    async def boom(**kw):
        raise RuntimeError("kubectl failed")
    reg.register("k8s.delete", RiskClass.ACT, "delete", handler=boom)
    try:
        asyncio.run(reg.invoke("k8s.delete"))
        assert False
    except RuntimeError as e:
        assert "kubectl failed" in str(e)
