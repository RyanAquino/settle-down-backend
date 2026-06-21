"""Tests for the receipt-agent factories.

Both factories build a pydantic-ai ``Agent`` from a provider-specific model via
the shared ``_build_agent`` helper. These tests exercise the assembly only — no
network is hit, since constructing the OpenAI/OpenRouter clients with a dummy
key does not make a request. Dummy keys are required because the factories
construct the client eagerly (a missing key would raise).
"""

import pytest
from django.test import override_settings
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel

from backend_api.dto.llm7_override import LLM7ChatModel
from backend_api.ocr import get_openrouter_receipt_agent, get_receipt_agent


@pytest.fixture(autouse=True)
def _clear_agent_caches():
    """Both factories are ``@lru_cache``d; clear so each test builds fresh
    against its own overridden settings (and never leaks an agent built with
    one test's keys/model into another)."""
    get_receipt_agent.cache_clear()
    get_openrouter_receipt_agent.cache_clear()
    yield
    get_receipt_agent.cache_clear()
    get_openrouter_receipt_agent.cache_clear()


@override_settings(LLM_API_KEY="test-key")
def test_default_factory_uses_llm7_model():
    agent = get_receipt_agent()
    assert isinstance(agent, Agent)
    # The existing path keeps the LLM7 parsing workaround.
    assert isinstance(agent.model, LLM7ChatModel)
    assert agent.model.model_name == "gpt-5-mini"


@override_settings(OPENROUTER_API_KEY="test-key")
def test_openrouter_factory_uses_clean_openai_model():
    agent = get_openrouter_receipt_agent()
    assert isinstance(agent, Agent)
    # OpenRouter returns OpenAI-compliant responses: a clean OpenAIChatModel,
    # NOT the LLM7 patch. The default model id is Gemini routed via OpenRouter.
    assert isinstance(agent.model, OpenAIChatModel)
    assert not isinstance(agent.model, LLM7ChatModel)
    assert agent.model.model_name == "google/gemini-2.5-flash-lite"


@override_settings(
    OPENROUTER_API_KEY="test-key", OPENROUTER_MODEL="anthropic/claude-3.5-sonnet"
)
def test_openrouter_model_id_is_configurable():
    agent = get_openrouter_receipt_agent()
    assert agent.model.model_name == "anthropic/claude-3.5-sonnet"


@override_settings(LLM_API_KEY="test-key", OPENROUTER_API_KEY="test-key")
def test_both_factories_register_translate_tool():
    # The shared _build_agent wires the translate tool onto both providers.
    for factory in (get_receipt_agent, get_openrouter_receipt_agent):
        agent = factory()
        assert "translate_jp_to_en_text" in agent._function_toolset.tools


@override_settings(OPENROUTER_API_KEY="test-key")
def test_openrouter_factory_is_cached():
    assert get_openrouter_receipt_agent() is get_openrouter_receipt_agent()
