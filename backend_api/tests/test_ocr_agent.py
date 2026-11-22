"""Tests for the receipt-agent factories.

Both factories build a pydantic-ai ``Agent`` from a provider-specific model via
the shared ``_build_agent`` helper. These tests exercise the assembly only — no
network is hit, since constructing the OpenAI/OpenRouter clients with a dummy
key does not make a request. Dummy keys are required because the factories
construct the client eagerly (a missing key would raise).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.test import override_settings
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel

from backend_api.dto.llm7_override import LLM7ChatModel
from backend_api.ocr import get_openrouter_receipt_agent, get_receipt_agent

# The id ``settledown/settings.py`` falls back to when OPENROUTER_MODEL is unset.
# Overridden onto the settings below rather than read from them: ``load_dotenv()``
# means a developer .env pointing at another model would otherwise fail this test
# locally while CI (which has no .env) passes.
DEFAULT_OPENROUTER_MODEL = "google/gemini-3-flash-preview"


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


@override_settings(
    OPENROUTER_API_KEY="test-key", OPENROUTER_MODEL=DEFAULT_OPENROUTER_MODEL
)
def test_openrouter_factory_uses_clean_openai_model():
    agent = get_openrouter_receipt_agent()
    assert isinstance(agent, Agent)
    # OpenRouter returns OpenAI-compliant responses: a clean OpenAIChatModel,
    # NOT the LLM7 patch. The default model id is Gemini routed via OpenRouter.
    assert isinstance(agent.model, OpenAIChatModel)
    assert not isinstance(agent.model, LLM7ChatModel)
    assert agent.model.model_name == DEFAULT_OPENROUTER_MODEL


@override_settings(
    OPENROUTER_API_KEY="test-key", OPENROUTER_MODEL="anthropic/claude-3.5-sonnet"
)
def test_openrouter_model_id_is_configurable():
    agent = get_openrouter_receipt_agent()
    assert isinstance(agent.model, OpenAIChatModel)
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


def _translate_tool():
    agent = get_openrouter_receipt_agent()
    return agent._function_toolset.tools["translate_jp_to_en_text"].function


@override_settings(OPENROUTER_API_KEY="test-key")
def test_translate_tool_returns_the_english_text():
    with patch("backend_api.ocr.Translator") as translator:
        translator.return_value.translate = AsyncMock(
            return_value=SimpleNamespace(text="rice ball")
        )
        result = asyncio.run(_translate_tool()("おにぎり"))

    assert result == "rice ball"
    translator.return_value.translate.assert_awaited_once_with("おにぎり", dest="en")


@override_settings(OPENROUTER_API_KEY="test-key")
def test_translate_tool_asks_the_model_to_retry_on_failure():
    with patch("backend_api.ocr.Translator") as translator:
        translator.return_value.translate = AsyncMock(side_effect=OSError("blocked"))

        with pytest.raises(ModelRetry, match="shorter chunk") as exc:
            asyncio.run(_translate_tool()("おにぎり"))

    assert isinstance(exc.value.__cause__, OSError)
