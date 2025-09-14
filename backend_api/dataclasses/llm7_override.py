from datetime import datetime

from openai.types import chat
from pydantic import ValidationError
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.models.openai import OpenAIChatModel


class LLM7ChatModel(OpenAIChatModel):
    def _process_response(self, response: chat.ChatCompletion | str):
        if not isinstance(response, chat.ChatCompletion):
            raise UnexpectedModelBehavior(
                "Invalid response from OpenAI chat completions endpoint, expected JSON data"
            )

        # --- 🔧 PATCH HERE: ensure choice.index is always int ---
        for i, choice in enumerate(response.choices):
            if getattr(choice, "index", None) is None:
                choice.index = i

        # Fallback: if created is missing
        if not response.created:
            response.created = int(datetime.utcnow().timestamp())

        try:
            response = chat.ChatCompletion.model_validate(response.model_dump())
        except ValidationError as e:
            raise UnexpectedModelBehavior(
                f"Invalid response from OpenAI chat completions endpoint: {e}"
            ) from e

        # ✅ call parent logic after patching
        return super()._process_response(response)
