"""Tests for the receipt-agent factories.

Both factories build a pydantic-ai ``Agent`` from a provider-specific model via
the shared ``_build_agent`` helper. These tests exercise the assembly only: no
network is hit, since constructing the OpenAI/OpenRouter clients with a dummy
key does not make a request. Dummy keys are required because the factories
construct the client eagerly (a missing key would raise).
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from django.test import override_settings
from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from receipts.llm7 import LLM7ChatModel
from receipts.ocr import (
    ScanAttempts,
    _build_agent,
    get_openrouter_receipt_agent,
    get_receipt_agent,
)
from receipts.tests import scans
from receipts.transcription import ReceiptScan

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
    with patch("receipts.ocr.Translator") as translator:
        translator.return_value.translate = AsyncMock(
            return_value=SimpleNamespace(text="rice ball")
        )
        result = asyncio.run(_translate_tool()("おにぎり"))

    assert result == "rice ball"
    translator.return_value.translate.assert_awaited_once_with("おにぎり", dest="en")


@override_settings(OPENROUTER_API_KEY="test-key")
def test_translate_tool_asks_the_model_to_retry_on_failure():
    with patch("receipts.ocr.Translator") as translator:
        translator.return_value.translate = AsyncMock(side_effect=OSError("blocked"))

        with pytest.raises(ModelRetry, match="shorter chunk") as exc:
            asyncio.run(_translate_tool()("おにぎり"))

    assert isinstance(exc.value.__cause__, OSError)


class _ChatCompletions:
    """A chat-completions endpoint that records requests and returns a tool call."""

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "object": "chat.completion",
                "created": 1_767_225_600,
                "model": body["model"],
                "provider": "Offline",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "final_result",
                                        "arguments": json.dumps(self.payload),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def _openrouter_gemini(endpoint):
    return OpenRouterModel(
        DEFAULT_OPENROUTER_MODEL,
        provider=OpenRouterProvider(api_key="test-key", http_client=endpoint.client()),
    )


def _llm7(endpoint):
    client = AsyncOpenAI(api_key="test-key", http_client=endpoint.client())
    return LLM7ChatModel("gpt-5-mini", provider=OpenAIProvider(openai_client=client))


def _objects(schema):
    """Every JSON-schema object node, nested definitions included."""
    if isinstance(schema, dict):
        if "properties" in schema:
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


def _defaults(schema):
    if isinstance(schema, dict):
        if "default" in schema:
            yield schema
        for value in schema.values():
            yield from _defaults(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _defaults(value)


@pytest.mark.parametrize("build", [_openrouter_gemini, _llm7], ids=["gemini", "llm7"])
class TestWireSchema:
    """What each provider path actually sends the model, captured offline."""

    def test_every_transcribed_field_is_required(self, build):
        # On the Gemini path a field with a default is optional on the wire,
        # so the model could leave out an amount without being asked again.
        sugi = scans.scan_payload(scans.vector("sugi"))
        endpoint = _ChatCompletions(sugi)

        result = _build_agent(build(endpoint)).run_sync(
            "here is the image receipt:", deps=ScanAttempts()
        )

        (request,) = endpoint.requests
        (tool,) = [
            tool
            for tool in request["tools"]
            if tool["function"]["name"] == "final_result"
        ]
        parameters = tool["function"]["parameters"]
        objects = list(_objects(parameters))
        assert len(objects) == 4  # receipt, item, adjustment, tax line
        for node in objects:
            assert set(node["required"]) == set(node["properties"])
        assert list(_defaults(parameters)) == []
        assert result.output == ReceiptScan.model_validate(sugi)
