"""What the OCR model transcribes from a receipt photo.

The model copies printed lines and never computes: every amount here is a figure
printed on the receipt. ``backend_api.receipt_allocation`` does the money math.

Shape rules, because on the OpenRouter/Gemini path ``strict=True`` does not
produce a strict schema:

- Every field is required and absence is ``null``. A field with a default is
  silently optional there, so the model could omit it without a retry.
- Enums are string ``Literal``s, and objects are lists of flat models.
- Nothing here may import Django: the pure allocation module depends on it.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, FiniteFloat

AdjustmentKind = Literal["discount", "points", "surcharge"]


class ScannedItem(BaseModel):
    item_order: int = Field(
        description="1-based position of this purchased line, in printed order"
    )
    japanese_name: str = Field(
        description="Item name as printed, without leading marks such as *, ※, A or B"
    )
    english_name: str = Field(description="English translation of the item name")
    quantity: int = Field(description="Quantity purchased on this line")
    printed_price: FiniteFloat = Field(
        description="Amount printed on the item's own line for the quantity shown, BEFORE any discount line. Copy it as printed; never subtract"
    )


class ScannedAdjustment(BaseModel):
    printed_label: str = Field(
        description="Label exactly as printed, e.g. 1品割引, 全品割引, クーポン, ポイント値引, サービス料"
    )
    kind: AdjustmentKind = Field(
        description="discount = any price reduction or coupon; points = loyalty points used to pay or to reduce the price; surcharge = a service charge or fee added on top"
    )
    amount: FiniteFloat = Field(
        description="Money amount printed on this line, as a positive number. Copy it as printed; never compute it"
    )
    percentage: FiniteFloat | None = Field(
        description="Percent printed on this line, or null if none is printed"
    )
    item_orders: list[int] = Field(
        description="item_order of the item(s) this line applies to, only when the receipt shows it (printed directly under one item, or naming its items). Empty when it applies to the whole receipt or the receipt does not say"
    )
    eligible_subtotal: FiniteFloat | None = Field(
        description="Printed base amount this line was calculated on (対象金額, 割引対象額), or null if none is printed"
    )


class ScannedTaxLine(BaseModel):
    rate: FiniteFloat | None = Field(
        description="Tax rate percent printed on this line, or null"
    )
    taxable_amount: FiniteFloat | None = Field(
        description="Taxable base printed for this rate (対象), or null"
    )
    tax_amount: FiniteFloat | None = Field(
        description="Tax amount printed for this rate, or null"
    )


class ReceiptScan(BaseModel):
    jp_shop_name: str = Field(description="Shop name as printed")
    en_shop_name: str = Field(description="Shop name in English")
    receipt_date: datetime | None = Field(
        description="Receipt date and time in Japan Standard Time, or null if none is printed"
    )
    decimal_places: int = Field(
        description="Decimal places of the receipt's currency: 0 for yen, won and Taiwan dollars, 2 for US dollars, euros and Hong Kong dollars"
    )
    items: list[ScannedItem] = Field(
        description="Every purchased line, in printed order"
    )
    adjustments: list[ScannedAdjustment] = Field(
        description="Every printed line that lowers or raises the amount to pay, in printed order, each exactly once. Empty if none"
    )
    tax_lines: list[ScannedTaxLine] = Field(
        description="One entry per printed tax line. Empty if none"
    )
    amount_due: FiniteFloat = Field(
        description="Final amount the customer had to pay after every discount, coupon and points line (支払合計, or 合計 when nothing is deducted after it)"
    )
