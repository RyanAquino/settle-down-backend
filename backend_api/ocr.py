"""Receipt OCR agents.

Provider-specific pydantic-ai Agents are built once each via lazy, cached
factories. Lazy (not module-level) so importing this module never constructs a
client/provider — which raises if the relevant API key is unset.

- ``get_receipt_agent``: OpenAI client + LLM7 model wrapper + translate tool.
- ``get_openrouter_receipt_agent``: OpenRouter provider (clean OpenAIChatModel).

Both delegate to ``_build_agent`` for the shared output schema, instructions,
retry budget, output validator, and translate tool — the only difference is the
underlying model/provider.
"""

import unicodedata
from functools import lru_cache

from django.conf import settings
from googletrans import Translator
from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from backend_api.dto.llm7_override import LLM7ChatModel
from backend_api.dto.scanned_receipt import ScannedReceipt

INSTRUCTIONS = """\
You are an expert receipt-reading system for Japanese and English receipts.
You receive a receipt as an image. Extract the fields defined by the output schema, following these rules:

- Languages: the receipt may be Japanese, English, or both. For every name (each item and the shop), provide BOTH the original Japanese text and an English translation. Never discard or overwrite the original Japanese.
- Amounts: report every amount in the receipt's own currency — do not convert. Strip currency marks (¥, 円, $, NT$, HK$, US$, €) and comma grouping (e.g. "1,200" -> 1200); digits may be full-width (１２３). Zero-decimal currencies (JPY, KRW, TWD) are whole integers — never invent a fractional part; currencies that carry cents (USD, EUR, HKD) keep the decimals as printed.
- Items: list each purchased line item. `cost` is the final price for that line AFTER item-specific discounts and BEFORE receipt-wide discounts or points, for the quantity shown (it is a line total, not a per-unit price). If a line has no price or 0, omit it.
- Discounts: when a discount line applies to the item directly above it, subtract it from that item's `cost`; record the savings in that item's `discount` field. A 1品割引 / １品割引 / 単品割引 is always an individual item coupon. For example, a 200 item followed by 1品割引 -30 is one item with cost 170 and discount 30. Never list those coupons in receipt_adjustments.
- Receipt adjustments: list each printed basket coupon or order discount (e.g. 全品割引) as kind `discount`, and the currency value of points actually spent (e.g. ポイント値引金額 or 利用ポイント) as `points_redemption`. Use positive deduction amounts. Keep these out of item costs. Exclude summaries of savings already counted on items, points earned/balances, eligibility subtotals (対象金額), tax breakdowns and payment-method lines. Never invent a deduction to close a gap.
- Eligibility: copy the eligible subtotal (対象金額) into eligible_subtotal when printed, independently of the discount amount. Give item_order references when the receipt identifies the eligible lines. Otherwise return an empty list. Never use the eligible subtotal as a price or deduction.
- Tax: set `tax_percentage` to the rate printed on the receipt (use 0 if none or multiple rates are shown).
- Total: copy the final printed amount payable after discounts and points (支払合計). Prefer it over an intermediate shopping total (お買物合計). Ignore cash tendered, change, tax bases and repeated payment confirmations. Preserve tax-inclusive prices. Never alter a line to make it balance.
- Date: report `receipt_date` in Japan Standard Time (JST). If the receipt shows no date, leave it unset.
"""


def validate_receipt_data(data: ScannedReceipt) -> ScannedReceipt:
    """Request a reread for malformed lines, totals or checkout deductions.

    The mobile reader reconciles the printed amounts and tax. Coupon scope is
    resolved from the accepted evidence during receipt normalization.
    """
    if data.total_amount < 0 or (
        data.total_amount == 0 and not data.receipt_adjustments
    ):
        raise ModelRetry(
            "total_amount must be the positive printed grand total unless fully discounted."
        )
    orders = {item.item_order for item in data.receipt_items}
    if len(orders) != len(data.receipt_items):
        raise ModelRetry("Give each purchased line a distinct item_order.")
    for adjustment in data.receipt_adjustments:
        label = "".join(unicodedata.normalize("NFKC", adjustment.printed_label).split())
        if any(mark in label for mark in ("1品割引", "単品割引", "1点割引")):
            raise ModelRetry(
                f"{adjustment.printed_label} is an item coupon. Subtract it from the immediately preceding "
                "purchased line, put the savings in that line's discount, and remove it from receipt_adjustments."
            )
        eligible = set(adjustment.item_orders)
        if len(eligible) != len(adjustment.item_orders) or not eligible.issubset(
            orders
        ):
            raise ModelRetry(
                "Each eligible item_order must reference a purchased line exactly once."
            )
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


def _build_agent(model: Model) -> Agent[None, ScannedReceipt]:
    """Assemble the receipt-reading agent around a provider-specific model.

    Everything except the underlying model/provider is shared: the output
    schema, instructions, retry budget, output validator, and translate tool.
    """
    agent = Agent(
        model=model,
        output_type=ToolOutput(ScannedReceipt, strict=True),
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
        except Exception as exc:
            # googletrans scrapes an unofficial endpoint and fails in many ways;
            # every one of them is worth a retry with a smaller input.
            raise ModelRetry(
                "Translation failed, please try with a shorter chunk of text."
            ) from exc

        translated: str = text_result.text
        return translated

    return agent


@lru_cache(maxsize=1)
def get_receipt_agent() -> Agent[None, ScannedReceipt]:
    """Build (once) the receipt agent backed by the OpenAI/LLM7 model.

    Talks to OpenAI by default; LLM7 is OpenAI-compatible, so pointing the
    client's ``base_url`` at https://api.llm7.io/v1 switches providers.
    """
    client = AsyncOpenAI(api_key=settings.LLM_API_KEY)
    model = LLM7ChatModel("gpt-5-mini", provider=OpenAIProvider(openai_client=client))
    return _build_agent(model)


@lru_cache(maxsize=1)
def get_openrouter_receipt_agent() -> Agent[None, ScannedReceipt]:
    """Build (once) the receipt agent backed by OpenRouter.

    Uses a clean ``OpenAIChatModel``: OpenRouter returns OpenAI-compliant
    responses, so the ``LLM7ChatModel`` index/created patch isn't needed.
    """
    model = OpenRouterModel(
        settings.OPENROUTER_MODEL,
        provider=OpenRouterProvider(api_key=settings.OPENROUTER_API_KEY),
    )
    return _build_agent(model)
