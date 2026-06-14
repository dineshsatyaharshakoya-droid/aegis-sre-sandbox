"""Tests for the LLM client wrapper (llm.py) with an injected fake client."""

import asyncio
import types

import aegis_sre.orchestrator.llm as llm


class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.seen = None

    async def create(self, **kwargs):
        self.seen = kwargs
        msg = types.SimpleNamespace(content=self._content)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])


class _FakeClient:
    def __init__(self, content='{"ok": true}'):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(content))


def test_chat_json_returns_message_content():
    fake = _FakeClient('{"verdict": "correct"}')
    out = asyncio.run(llm.chat_json("m", "sys", "user", client=fake))
    assert out == '{"verdict": "correct"}'


def test_chat_json_requests_json_mode_and_passes_prompts():
    fake = _FakeClient()
    asyncio.run(llm.chat_json("hermes", "S", "U", temperature=0.0, client=fake))
    sent = fake.chat.completions.seen
    assert sent["model"] == "hermes"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["messages"][0] == {"role": "system", "content": "S"}
    assert sent["messages"][1] == {"role": "user", "content": "U"}


def test_get_llm_client_is_singleton_and_resettable(monkeypatch):
    created = []

    class _AsyncOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _AsyncOpenAI
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    llm.reset_llm_client()
    c1 = llm.get_llm_client()
    c2 = llm.get_llm_client()
    assert c1 is c2 and len(created) == 1  # built once
    llm.reset_llm_client()
    llm.get_llm_client()
    assert len(created) == 2  # rebuilt after reset
    llm.reset_llm_client()
