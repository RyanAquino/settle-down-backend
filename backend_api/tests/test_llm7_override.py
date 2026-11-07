"""LLM7ChatModel repairs LLM7's non-compliant chat completions before parsing."""

import pytest
from openai.types import chat
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import TextPart
from pydantic_ai.providers.openai import OpenAIProvider

from backend_api.dto.llm7_override import LLM7ChatModel


@pytest.fixture
def model():
    return LLM7ChatModel("gpt-5-mini", provider=OpenAIProvider(api_key="test-key"))


def _completion(*, created, choices, model_name="gpt-5-mini"):
    # model_construct skips validation, which is how a non-compliant LLM7
    # payload reaches pydantic-ai in the first place.
    return chat.ChatCompletion.model_construct(
        id="cmpl-1",
        object="chat.completion",
        created=created,
        model=model_name,
        choices=choices,
    )


def _choice(index, content):
    return Choice.model_construct(
        index=index,
        finish_reason="stop",
        message=ChatCompletionMessage(role="assistant", content=content),
    )


def test_rejects_a_non_json_response(model):
    with pytest.raises(UnexpectedModelBehavior, match="expected JSON data"):
        model._process_response("<html>gateway error</html>")


def test_fills_missing_choice_index_and_created(model):
    completion = _completion(created=None, choices=[_choice(None, "hello")])

    response = model._process_response(completion)

    assert response.parts == [TextPart(content="hello")]
    assert completion.choices[0].index == 0
    assert completion.created > 0


def test_keeps_compliant_fields_untouched(model):
    completion = _completion(created=1_700_000_000, choices=[_choice(0, "hi")])

    model._process_response(completion)

    assert completion.created == 1_700_000_000
    assert completion.choices[0].index == 0


def test_reports_payloads_that_stay_invalid_after_patching(model):
    completion = _completion(created=1, choices=[_choice(0, "hi")], model_name=None)

    with pytest.raises(UnexpectedModelBehavior, match="Invalid response"):
        model._process_response(completion)
