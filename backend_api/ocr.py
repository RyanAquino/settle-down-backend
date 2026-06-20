"""Receipt OCR agent.

The pydantic-ai Agent (OpenAI client + LLM7 model + translate tool) is built
once via a lazy, cached factory. Lazy (not module-level) so importing this
module never constructs AsyncOpenAI — which raises if LLM_API_KEY is unset.
"""

from functools import lru_cache

from django.conf import settings
from googletrans import Translator
from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry, ToolOutput
from pydantic_ai.providers.openai import OpenAIProvider

from .dataclasses.llm7_override import LLM7ChatModel
from .dataclasses.receipt_item import ReceiptData

INSTRUCTIONS = """\
You are an expert receipt-reading system for Japanese and English receipts.
You receive a receipt as an image. Extract the fields defined by the output schema, following these rules:

- Languages: the receipt may be Japanese, English, or both. For every name (each item and the shop), provide BOTH the original Japanese text and an English translation. Never discard or overwrite the original Japanese.
- Currency: every amount is Japanese yen (JPY), a whole integer. Strip thousands separators (e.g. "1,200" -> 1200). Never invent fractional yen.
- Items: list each purchased line item. `cost` is the final price for that line AFTER any discount, for the quantity shown (it is a line total, not a per-unit price). If a line has no price or 0, omit it.
- Discounts: when a discount line applies to the item directly above it, subtract it from that item's `cost`; do not emit discounts as their own items.
- Tax: set `tax_percentage` to the rate printed on the receipt (use 0 if none is shown).
- Total: copy `total_amount` exactly as printed — it is authoritative. Do not recompute or adjust it. The printed total may already include tax or have tax added on top; preserve it verbatim and make the item costs consistent with it.
- Date: report `receipt_date` in Japan Standard Time (JST). If the receipt shows no date, leave it unset.
"""


def validate_receipt_data(data: ReceiptData) -> ReceiptData:
    """Catch gross extraction errors without rejecting plausible receipts.

    Deliberately does NOT enforce that item costs sum to total_amount: JP
    receipts mix 8%/10% tax (not representable here, per CLAUDE.md), so a strict
    sum check would falsely reject common receipts and exhaust retries -> 500.
    total_amount is trusted input; we only guard against obviously-broken output.
    """
    if data.total_amount <= 0:
        raise ModelRetry("total_amount must be the positive printed grand total.")
    if not data.receipt_items:
        raise ModelRetry(
            "No line items found; re-read the image and list each priced item."
        )
    for item in data.receipt_items:
        if item.cost < 0:
            raise ModelRetry(
                f"Item '{item.english_name}' has negative cost; costs are "
                "post-discount and must be >= 0."
            )
    return data


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
        output_type=ToolOutput(ReceiptData, strict=True),
        instructions=INSTRUCTIONS,
        retries=2,
    )
    agent.output_validator(validate_receipt_data)

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
