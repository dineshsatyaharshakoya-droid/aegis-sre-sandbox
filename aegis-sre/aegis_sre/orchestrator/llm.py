"""
LLM client for the Aegis repair swarm.

Aegis talks to models over the **OpenAI Chat Completions API**. A local Ollama
instance exposes an OpenAI-compatible endpoint at `http://localhost:11434/v1`,
so the same code runs fully on-prem / air-gapped (no SaaS egress) or against any
OpenAI-compatible provider — selected entirely by configuration.

  - base URL / api key / models  -> `aegis_sre.config.Settings`
  - executor model (proposes patches): `settings.executor_model`  (e.g. hermes3:8b)
  - reviewer model (validates):        `settings.reviewer_model`  (e.g. qwen2.5-coder:7b)

The `AsyncOpenAI` import is deferred into `get_llm_client()` so this module
imports without the `openai` package present (keeps the infra/test layers light),
and so a fake client can be injected in tests.
"""

from __future__ import annotations

from typing import Optional

from aegis_sre.config import get_settings
from aegis_sre.telemetry.logger import logger

_client = None  # process-wide singleton AsyncOpenAI


def get_llm_client():
    """Return the shared AsyncOpenAI client pointed at the configured endpoint."""
    global _client
    if _client is None:
        from openai import AsyncOpenAI  # lazy import

        s = get_settings()
        _client = AsyncOpenAI(
            base_url=s.llm_base_url,
            api_key=s.llm_api_key,
            timeout=s.llm_timeout_seconds,
        )
        logger.info("llm_client_initialized", base_url=s.llm_base_url,
                    executor=s.executor_model, reviewer=s.reviewer_model)
    return _client


def reset_llm_client() -> None:
    """Drop the cached client (tests / config reload)."""
    global _client
    _client = None


async def chat_json(
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.0,
    client=None,
) -> str:
    """Run a JSON-mode chat completion and return the raw message content.

    `temperature=0.0` keeps patch/review generation as deterministic as the model
    allows. `response_format=json_object` asks the endpoint for strict JSON; Ollama
    honours this for models that support structured output.
    """
    client = client or get_llm_client()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    return response.choices[0].message.content
