"""Receipt OCR agents.

Provider-specific pydantic-ai Agents are built once each via lazy, cached
factories. Lazy (not module-level) so importing this module never constructs a
client/provider, which raises if the relevant API key is unset.

- ``get_receipt_agent``: OpenAI client + LLM7 model wrapper + translate tool.
- ``get_openrouter_receipt_agent``: OpenRouter provider (clean OpenAIChatModel).
- ``read_receipt``: reads one photo with the OpenRouter agent and returns the
  best attempt of the run.

Both delegate to ``_build_agent`` for the shared output schema, instructions,
retry budget, output validator, and translate tool. The only difference is the
underlying model/provider.

The model only transcribes what is printed (``ReceiptScan``). It never nets a
discount into a price or computes a total: ``receipts.allocation``
does all of the money math, and the output validator uses it to decide whether
a transcription is worth reading again.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache

from django.conf import settings
from googletrans import Translator
from openai import AsyncOpenAI
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from receipts.allocation import (
    Allocation,
    TranscriptionError,
    allocate_scan,
)
from receipts.llm7 import LLM7ChatModel
from receipts.transcription import ReceiptScan

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
You are an expert receipt-reading system for Japanese and English receipts.
You receive a receipt as an image. Transcribe it into the output schema. Copy what is printed; never calculate, net or adjust an amount yourself.

- Languages: the receipt may be Japanese, English, or both. For every item and the shop, give the name as printed and an English translation. Never discard the original text.
- Amounts: report every amount in the receipt's own currency; do not convert. Strip currency marks (¥, 円, $, NT$, HK$, US$, €) and comma grouping ("1,200" -> 1200); digits may be full-width (１２３). Every amount is a positive number: the field says what it means, so drop printed minus signs, triangle marks and brackets. `decimal_places` is 0 for currencies without minor units (yen, won, Taiwan dollars) and 2 for currencies with cents (US dollars, euros, Hong Kong dollars); amounts keep the decimals they are printed with.
- Items: list every purchased line in printed order, numbered from 1 in `item_order`. `printed_price` is the amount printed on the item's own line for the quantity shown, BEFORE any discount line; never subtract anything from it. Keep an item even when a coupon makes it free. Discount lines, totals, tax lines and payment lines are never items. Marks before a name, such as *, ※, 軽, A or B, are not part of the name.
- Adjustments: list EVERY printed line that lowers or raises the amount to pay, in printed order, each exactly once: item discounts (値引, 割引, 1品割引, 見切り, 半額), multi-item and all-item discounts (まとめ値引, セット割, 全品割引), coupons (クーポン), loyalty points used (ポイント値引, ポイント利用, ポイント支払) and surcharges (サービス料, service charge). `amount` is the money amount printed on that line and `percentage` is the percent printed on it, if any. Never compute one from the other.
- Scope: fill `item_orders` only when the receipt shows which items a line applies to. A discount printed directly under one item applies to that item. A set discount applies to the items it names. Leave `item_orders` empty when the line applies to the whole receipt or the receipt does not identify its items; never guess. When the receipt prints the base amount a line was calculated on (対象金額, 割引対象額), copy it into that line's `eligible_subtotal`. The base line itself is not an item, a total or an adjustment.
- Not adjustments: subtotals and totals (小計, 合計, お買物合計, 支払合計), tax lines, base-amount lines, points earned or point balances, and payment lines (現金, クレジット, 電子マネー, barcode or QR payments, 商品券, お預り, お釣り). Points used to pay are an adjustment of kind `points`.
- Total: `amount_due` is the final amount the customer had to pay after every discount, coupon and points line (支払合計, or 合計 when nothing is deducted after it).
- Tax: copy each printed tax line into `tax_lines` with its rate, its taxable base (対象) and its tax amount, using null for what is not printed. A tax amount printed without a rate (e.g. "Tax: 10.00") is still a tax line. A percent printed on a discount line is a discount rate, never a tax rate.
- Date: report `receipt_date` in Japan Standard Time. If the receipt shows no date, use null.
"""

# Best first: a scan that reconciles, then one that only needs a person to
# confirm which items an adjustment covers, then one that does not add up.
_STATUS_RANK = {"reconciled": 0, "needs_review": 1, "mismatch": 2}


@dataclass
class ScanAttempts:
    """Every transcription of one photo that could be read as money.

    Passed to the agent as its run dependencies, so the output validator can
    return the best attempt of the run rather than the last one, and the
    endpoint can still answer with it if a later read fails outright.
    """

    attempts: list[tuple[ReceiptScan, Allocation]] = field(default_factory=list)
    reread_requested: bool = False

    def record(self, scan: ReceiptScan, allocation: Allocation) -> None:
        self.attempts.append((scan, allocation))

    def best(self) -> ReceiptScan | None:
        """The best-ranked attempt, the earliest among equals."""
        if not self.attempts:
            return None
        scan, _ = min(
            self.attempts, key=lambda attempt: _STATUS_RANK[attempt[1].status]
        )
        return scan


def _reread_message(allocation: Allocation, decimal_places: int) -> str:
    gap = format(Decimal(abs(allocation.difference)).scaleb(-decimal_places), "f")
    return (
        "The transcription does not add up: the printed prices, the adjustments "
        f"you listed and the printed totals leave a difference of {gap}. "
        "Read the receipt again. "
        "Copy every purchased line and every discount, coupon, points and "
        "surcharge line exactly once, as printed, and make sure amount_due is the "
        "final amount paid. Tax lines belong in tax_lines, never in adjustments. "
        "Do not add a line that is not printed to close the gap."
    )


def validate_receipt_scan(
    ctx: RunContext[ScanAttempts], scan: ReceiptScan
) -> ReceiptScan:
    """Ask for a re-read only when it can help, and keep the best attempt.

    - A transcription that cannot be read as money (no items, a negative
      price, no amount due) is read again on the shared retry budget.
    - One that does not add up gets one targeted re-read.
    - Anything else is accepted. An adjustment whose items the printed numbers
      cannot settle is for a person to confirm, not for another read.
    """
    try:
        receipt, allocation = allocate_scan(scan)
    except TranscriptionError as error:
        raise ModelRetry(str(error)) from error

    ctx.deps.record(scan, allocation)
    if allocation.status == "reconciled":
        return scan
    if (
        allocation.status == "mismatch"
        and not ctx.deps.reread_requested
        and not ctx.last_attempt
    ):
        ctx.deps.reread_requested = True
        raise ModelRetry(_reread_message(allocation, receipt.decimal_places))
    return ctx.deps.best() or scan


def _build_agent(model: Model) -> Agent[ScanAttempts, ReceiptScan]:
    """Assemble the receipt-reading agent around a provider-specific model.

    Everything except the underlying model/provider is shared: the output
    schema, instructions, retry budget, output validator, and translate tool.
    """
    agent = Agent(
        model=model,
        deps_type=ScanAttempts,
        output_type=ToolOutput(ReceiptScan, strict=True),
        instructions=INSTRUCTIONS,
        retries=2,
    )
    agent.output_validator(validate_receipt_scan)

    @agent.tool_plain
    async def translate_jp_to_en_text(text: str) -> str:
        """Translate Japanese text to English text."""

        try:
            # The Translator owns an HTTP client; leaving the block closes it,
            # or every call would leave its connection open.
            async with Translator() as translator:
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
def get_receipt_agent() -> Agent[ScanAttempts, ReceiptScan]:
    """Build (once) the receipt agent backed by the OpenAI/LLM7 model.

    Talks to OpenAI by default; LLM7 is OpenAI-compatible, so pointing the
    client's ``base_url`` at https://api.llm7.io/v1 switches providers.
    """
    client = AsyncOpenAI(api_key=settings.LLM_API_KEY)
    model = LLM7ChatModel("gpt-5-mini", provider=OpenAIProvider(openai_client=client))
    return _build_agent(model)


@lru_cache(maxsize=1)
def get_openrouter_receipt_agent() -> Agent[ScanAttempts, ReceiptScan]:
    """Build (once) the receipt agent backed by OpenRouter.

    Uses a clean ``OpenAIChatModel``: OpenRouter returns OpenAI-compliant
    responses, so the ``LLM7ChatModel`` index/created patch isn't needed.
    """
    model = OpenRouterModel(
        settings.OPENROUTER_MODEL,
        provider=OpenRouterProvider(api_key=settings.OPENROUTER_API_KEY),
    )
    return _build_agent(model)


# Brands a phone writes in a HEIC photo's "ftyp" box (bytes 8-12).
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def image_media_type(data: bytes) -> str:
    """The media type of a receipt photo, read from its first bytes.

    Uploads are phone photos: JPEG from either camera, PNG screenshots, WebP
    from some Android galleries, or HEIC from iPhone and Android HEIF photos.
    The bytes decide, because the type a phone declares is often generic.
    Anything else is sent as JPEG, as every upload was before.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "image/heic"
    return "image/jpeg"


async def read_receipt(data: bytes) -> ReceiptScan:
    """Transcribe a receipt photo, answering with the best attempt of the run.

    If a later read, or the model call itself, fails after an earlier read was
    already usable as money, that earlier read is the answer rather than an
    error. With no usable read, the failure propagates.
    """
    attempts = ScanAttempts()
    try:
        result = await get_openrouter_receipt_agent().run(
            [
                "here is the image receipt:",
                BinaryContent(data=data, media_type=image_media_type(data)),
            ],
            deps=attempts,
        )
    except Exception:
        best = attempts.best()
        if best is None:
            raise
        logger.warning(
            "Receipt read failed after a usable attempt; using the best attempt",
            exc_info=True,
        )
        return best
    return result.output
