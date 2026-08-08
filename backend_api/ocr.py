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

from functools import lru_cache

from django.conf import settings
from googletrans import Translator
from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from .dto.llm7_override import LLM7ChatModel
from .dto.receipt_item import ReceiptData

INSTRUCTIONS = """\
You are an expert receipt-reading system for receipts from Japan, Taiwan, and Hong Kong
(Japanese, Traditional Chinese, and English text, often mixed on one receipt).
You receive a receipt as an image. Extract the fields defined by the output schema, following these rules:

Names and languages:
- `japanese_name` and `jp_shop_name` hold the ORIGINAL text exactly as printed — whether it is
  Japanese, Chinese, or English. Copy it verbatim; never translate it into Japanese.
- `english_name` / `en_shop_name`: if the receipt itself prints an English version, use that;
  otherwise translate the original (the translate tool can help).
- The shop name is the store/brand printed at the top (with branch if shown) — never a document
  title (レシート, 領収書, 統一發票, 電子發票證明聯, 交易明細, 收據, Invoice) and never the
  customer/recipient name (宛名, 上様, 買方).

Items:
- List each purchased line item. `cost` is the final price for that line AFTER any discount, for
  the quantity shown (it is a line total, not a per-unit price). If a line has no price or 0, omit it.
- Quantity may be printed on its own row (e.g. 「2コX98」, 「2個×@98」, 數量 2); @ marks the UNIT
  price. Use the extended line amount as `cost` and do not emit the quantity/unit-price row as its
  own item. Default quantity to 1.
- Fees inside the total ARE items (so the bill-splitter can distribute them): service charge
  (服務費 / 加一 / SC / Service Charge — usually 10% / サービス料), per-person table or tea charges
  (お通し, 席料, チャージ, 茶位, 茶芥 — quantity is often the number of diners), 深夜料金,
  bag charge (膠袋 / Bag Charge), lodging taxes (宿泊税, 入湯税, HK hotel accommodation tax).
  Always use the exact printed amount — never recompute a service charge from its percentage
  (it is often based on the pre-discount subtotal).
- NEVER treat these as items or amounts: cash tendered (お預り, 現金, Cash — often a round number
  larger than the total), change (お釣り, 找零, 找續, 找贖, Change), payment-method lines
  (クレジット, ○○Pay, 信用卡, Octopus 八達通) and stored-value balance (餘額 / Remaining Value),
  item-count lines (5点), points earned or balance (付与ポイント, ポイント残高), tax breakdown
  lines (8%対象, 10%対象, 内消費税等, 銷售額, 稅額, 營業稅), invoice/lottery numbers (Taiwan
  invoice number AB-12345678, 4-digit 隨機碼), business registration numbers (統一編號/統編
  8 digits, 登録番号 T+13 digits), table number (檯號), diner count (人數/PAX),
  register/transaction/bill numbers, barcode or QR data, carrier codes starting with '/', and
  thank-you footers.
- Strip tax-category markers from item names: ※ / * / 軽 (Japan: reduced 8% rate),
  TX / TZ / 應稅 / 免稅 (Taiwan tax categories). They are flags, not part of the name.

Discounts:
- A negative line (leading -, trailing minus like 30-, △ or ▲) labeled
  値引/割引/クーポン/折扣/折讓/折抵/折價/優惠 directly below an item applies to THAT item:
  subtract it into the item's `cost`, record the positive amount in `discount`, and do not emit
  the discount line as an item. Points SPENT (ポイント利用/ポイント値引) are a discount too.
- Distribute receipt-level discounts (printed after the subtotal) proportionally across the items
  so items stay consistent with the printed total; never output a negative `cost`.
- N折 notation means paying N/10 of the price: 9折 = 10% off, 85折 = 15% off, 8折 = 20% off
  (never N% off).

Tax (`tax_percentage`):
- `tax_percentage` is the rate that must be ADDED ON TOP of the item costs to reach the total.
  Decide by reconciling, not by the mere presence of tax lines: if the printed item costs already
  sum to (about) the printed total, the total is tax-INCLUSIVE → set 0 and treat all printed tax
  lines as informational.
- Tax-inclusive is the norm: virtually all Taiwan B2C receipts (5% VAT is embedded in prices by
  law, even when a 銷售額/稅額 breakdown is printed), Japanese 内税/税込 receipts, and ALL
  Hong Kong receipts.
- Set a non-zero rate ONLY when the receipt genuinely adds tax between subtotal and total (item
  lines are pre-tax): Japanese 外税/税抜 receipts with a single rate → 8 or 10 as printed;
  Taiwanese 三聯式 B2B invoices where items sum to the pre-tax 銷售額 → 5, but only when every
  line is taxable (應稅/TX). If a pre-tax Taiwanese invoice mixes taxable and exempt (免稅) lines,
  fold 5% into each taxable item's cost and set 0 (稅額 covers only the taxable portion).
- Mixed-rate Japanese receipts (both 8%対象 and 10%対象 blocks) cannot be represented by one
  rate: use tax-inclusive line costs and set 0; if items are printed pre-tax, fold each item's
  own tax into its cost instead.
- Hong Kong has NO sales tax / VAT / GST of any kind: `tax_percentage` is always 0 there. Any 10%
  line is a service charge (an item, see above); do not hallucinate a tax.
- Never put a service-charge percentage into `tax_percentage`.

Total:
- Copy `total_amount` exactly as printed from the grand-total line — 合計/総合計 (JP), 總計 (TW),
  總數/合共/Total (HK) — it is authoritative. Do not recompute or adjust it, and never take it
  from cash tendered, change, 小計 (subtotal), 銷售額, or any nearby ID number (on Taiwan
  e-invoices the 4-digit 隨機碼 sits directly above 總計).

Amounts:
- JPY and TWD amounts are whole integers (only unit prices ever carry decimals); HKD POS amounts
  have 2 decimals, handwritten HK bills whole dollars. Strip currency marks (¥ — may be
  OCR-garbled as a backslash, 円, 元, $, NT$, HK$) and comma grouping; digits may be full-width
  (１２３). A small 捨零/Rounding line on HK cash bills is a real adjustment inside the total.

Date (`receipt_date`):
- Always convert to the Gregorian calendar. Taiwan uses ROC/Minguo years (2-3 digit year, e.g.
  113/05/12 or 113年5月12日): add 1911 to the year (113 → 2024). Prefer the 開立時間/發票日期
  line, and NEVER use the big lottery-period header 「NNN年MM-MM月」 as the date. Japanese eras:
  令和/R year + 2018 (R7.5.12 → 2025-05-12), 平成/H + 1988, 昭和/S + 1925; a bare 2-digit year on
  a modern Japanese receipt is almost always Reiwa. Hong Kong numeric dates are DAY-FIRST:
  05/08/2026 = 5 August 2026. If the receipt shows no date, set `receipt_date` to null — never
  guess one.

Documents without item lines:
- The Taiwan 電子發票證明聯 (small slip with two QR codes) usually carries NO item lines — they
  are on a separate 交易明細 slip. If no itemized section is visible, do NOT invent items: emit a
  single item for the full amount, named from the shop or context. Same for a Japanese handwritten
  領収書 (one lump amount + a 但し書き memo): emit one item named from the memo. Never fabricate
  item names, shop names, or dates that are not on the receipt — if no shop name is printed
  (common on handwritten bills), use an empty string for both shop-name fields.

Before answering, sanity-check: the extracted item costs (plus tax only if you set a non-zero
`tax_percentage`) should account for the printed total; if they do not, re-check quantity rows,
discount attachment, and whether tax is included.
"""


def validate_receipt_data(data: ReceiptData) -> ReceiptData:
    """Catch gross extraction errors without rejecting plausible receipts.

    Deliberately does NOT enforce that item costs sum to total_amount: JP
    receipts mix 8%/10% tax (not representable here, per CLAUDE.md), HK service
    charges are often computed on the pre-discount subtotal, and TW/HK rounding
    lines shift totals by a few cents — a strict sum check would falsely reject
    common receipts and exhaust retries -> 500. total_amount is trusted input;
    we only guard against obviously-broken output.
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


def _build_agent(model: Model) -> Agent:
    """Assemble the receipt-reading agent around a provider-specific model.

    Everything except the underlying model/provider is shared: the output
    schema, instructions, retry budget, output validator, and translate tool.
    """
    agent = Agent(
        model=model,
        output_type=ToolOutput(ReceiptData, strict=True),
        instructions=INSTRUCTIONS,
        retries=2,
    )
    agent.output_validator(validate_receipt_data)

    @agent.tool_plain
    async def translate_to_en_text(text: str) -> str:
        """Translate text to English text."""

        translator = Translator()
        try:
            text_result = await translator.translate(text, dest="en")
        except Exception:
            raise ModelRetry(
                "Translation failed, please try with a shorter chunk of text."
            )

        return text_result.text

    return agent


@lru_cache(maxsize=1)
def get_receipt_agent() -> Agent:
    """Build (once) the receipt agent backed by the OpenAI/LLM7 model."""
    client = AsyncOpenAI(
        # base_url="https://api.llm7.io/v1",
        api_key=settings.LLM_API_KEY,
    )
    model = LLM7ChatModel("gpt-5-mini", provider=OpenAIProvider(openai_client=client))
    return _build_agent(model)


@lru_cache(maxsize=1)
def get_openrouter_receipt_agent() -> Agent:
    """Build (once) the receipt agent backed by OpenRouter.

    Uses a clean ``OpenAIChatModel``: OpenRouter returns OpenAI-compliant
    responses, so the ``LLM7ChatModel`` index/created patch isn't needed.
    """
    model = OpenRouterModel(
        settings.OPENROUTER_MODEL,
        provider=OpenRouterProvider(api_key=settings.OPENROUTER_API_KEY),
    )
    return _build_agent(model)
