from datetime import UTC, datetime
from typing import override

from openai.types import chat
from pydantic import ValidationError
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.openai import OpenAIChatModel


class LLM7ChatModel(OpenAIChatModel):
    """OpenAI chat model that tolerates LLM7's non-compliant completions.

    LLM7 can omit ``choices[].index`` and ``created``, which the OpenAI schema
    requires. ``@override`` makes mypy fail if pydantic-ai renames the hook.
    """

    @override
    def _process_response(self, response: chat.ChatCompletion | str) -> ModelResponse:
        if not isinstance(response, chat.ChatCompletion):
            raise UnexpectedModelBehavior(
                "Invalid response from OpenAI chat completions endpoint, expected JSON data"
            )

        # Ensure choice.index is always an int.
        for i, choice in enumerate(response.choices):
            if getattr(choice, "index", None) is None:
                choice.index = i

        # Fallback: if created is missing
        if not response.created:
            response.created = int(datetime.now(UTC).timestamp())

        try:
            response = chat.ChatCompletion.model_validate(response.model_dump())
        except ValidationError as e:
            raise UnexpectedModelBehavior(
                f"Invalid response from OpenAI chat completions endpoint: {e}"
            ) from e

        # Call the parent logic after patching.
        return super()._process_response(response)
