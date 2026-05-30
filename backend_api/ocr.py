"""Receipt OCR agent.

The pydantic-ai Agent (OpenAI client + LLM7 model + translate tool) is built
once via a lazy, cached factory. Lazy (not module-level) so importing this
module never constructs AsyncOpenAI — which raises if LLM_API_KEY is unset.
"""

from functools import lru_cache

from django.conf import settings
from googletrans import Translator
from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.providers.openai import OpenAIProvider

from .dataclasses.llm7_override import LLM7ChatModel
from .dataclasses.receipt_item import ReceiptData

SYSTEM_PROMPT = """
            You are an expert system for reading receipts.
            You will be given receipts in a form of image. The text in the image receipt may be in Japanese and/or English.
            Your task it to extract the following text information from the provided image.
            Always translate the text to English before processing.
            if there is no cost on a particular receipt item, then don't include it.

            1. Receipt items which are listed in the receipt containing the english or japanese item name, item order, cost, quantity, and discount if any.
                a. If a discount applies, subtract it from the item directly above. Do not include discounts as receipt items.
            2. English and Japanese  name of the shop.
            3. Tax percentage if any.
            4. Total cost amount of all items in the receipt.
            5. Datetime of the receipt.

            The total amount in the receipt will always be correct. Make sure the receipt items you extracted add up to the total amount.
            It might be possible that the total cost already includes tax.
        """


@lru_cache(maxsize=1)
def get_receipt_agent() -> Agent:
    """Build (once) and return the receipt-reading agent."""
    client = AsyncOpenAI(
        # base_url="https://api.llm7.io/v1",
        api_key=settings.LLM_API_KEY,
    )
    model = LLM7ChatModel("gpt-5-mini", provider=OpenAIProvider(openai_client=client))
    agent = Agent(
        model=model,
        output_type=ReceiptData,
        system_prompt=SYSTEM_PROMPT,
    )

    @agent.tool_plain
    async def translate_jp_to_en_text(text: str) -> str:
        """Translate Japanese text to English text."""

        translator = Translator()
        try:
            text_result = await translator.translate(text, dest="en")
        except Exception:
            raise ModelRetry(
                "Translation failed, please try with a shorter chunk of text."
            )

        return text_result.text

    return agent
