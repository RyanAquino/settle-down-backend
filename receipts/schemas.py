from datetime import datetime
from typing import Literal

from ninja import Schema
from pydantic import Field, FiniteFloat

AdjustmentKind = Literal["discount", "points", "surcharge"]


# The scan response is the public contract the mobile app is built against. It
# is deliberately separate from the DTO the OCR model fills in, so rewording a
# prompt-facing field description never changes the API contract.


class OCRReceiptItemOut(Schema):
    english_name: str = Field(description="The item name in English")
    japanese_name: str = Field(description="The item name as printed on the receipt")
    item_order: int = Field(
        description="The position of the item on the receipt, from 1 in printed order"
    )
    cost: float = Field(
        description=(
            "What this line cost after its share of every discount, coupon, points "
            "and surcharge, for the quantity shown; before tax when tax is added on "
            "top. 0 when the receipt did not reconcile: see list_price and "
            "reconciliation"
        )
    )
    quantity: int = Field(description="The quantity purchased on this line")
    discount: float = Field(
        description=(
            "Everything taken off this line's printed price, its own discounts and "
            "its share of receipt-wide ones, already subtracted from cost"
        )
    )
    list_price: float = Field(description="The price printed on the item's own line")


class ReceiptAllocationShareOut(Schema):
    item_order: int
    amount: float = Field(description="What this item took of the adjustment")


class ReceiptAdjustmentOut(Schema):
    printed_label: str = Field(description="The label as printed, e.g. 全品割引")
    kind: AdjustmentKind = Field(
        description="discount and points lower the amount to pay, surcharge raises it"
    )
    amount: float = Field(description="The printed amount, always positive")
    percentage: float | None = Field(description="The printed percent, if any")
    eligible_subtotal: float | None = Field(
        description="The printed base amount it was calculated on, if any"
    )
    item_orders: list[int] = Field(
        description="The items it applies to, or the items the receipt names when unresolved"
    )
    status: Literal["verified", "assumed", "unresolved"] = Field(
        description=(
            "verified: the printed numbers prove which items it covers; assumed: "
            "taken from the receipt's layout or applied to the whole receipt; "
            "unresolved: a person must choose its items"
        )
    )
    allocations: list[ReceiptAllocationShareOut] = Field(
        description="What each item took, when the receipt reconciled; otherwise empty"
    )


class ReceiptTaxLineOut(Schema):
    rate: float | None
    taxable_amount: float | None
    tax_amount: float | None


class ReconciliationOut(Schema):
    status: Literal["reconciled", "needs_review", "mismatch"] = Field(
        description=(
            "reconciled: the costs add up to the printed totals; needs_review: an "
            "adjustment's items must be chosen by a person; mismatch: the printed "
            "lines do not add up to the amount due"
        )
    )
    difference: float = Field(
        description="The unexplained difference when the status is mismatch, else 0"
    )


class OCRReceiptPostOut(Schema):
    receipt_items: list[OCRReceiptItemOut] = Field(
        description="The purchased items, in printed order"
    )
    en_shop_name: str = Field(description="The shop name in English")
    jp_shop_name: str = Field(description="The shop name as printed on the receipt")
    tax_percentage: float = Field(
        description=(
            "The printed non-zero tax rate, or the non-zero one with the largest "
            "taxable base, or 0. "
            "When the costs add up to total_amount it is for display only; "
            "otherwise it is the rate added on top of the costs"
        )
    )
    total_amount: float = Field(
        description=(
            "What the group splits: the amount paid after every discount, coupon "
            "and points. The costs add up to it, or do with tax_percentage added"
        )
    )
    receipt_date: datetime = Field(description="The date of the receipt")
    receipt_image_url: str = Field(description="The url of the uploaded receipt image")
    decimal_places: int = Field(
        description="Decimal places of the receipt's currency: 0 for yen, 2 for cents"
    )
    adjustments: list[ReceiptAdjustmentOut] = Field(
        description="Every printed line that changed the amount to pay, in printed order"
    )
    tax_lines: list[ReceiptTaxLineOut] = Field(description="The printed tax lines")
    reconciliation: ReconciliationOut


# A correction sends back what the scan returned, with the items each
# adjustment applies to as a person chose them; unknown fields are ignored, so
# the scan's own objects can be posted back as they are.


class ReceiptCorrectionItemIn(Schema):
    item_order: int
    list_price: FiniteFloat


class ReceiptCorrectionAdjustmentIn(Schema):
    printed_label: str = Field(max_length=200)
    kind: AdjustmentKind
    amount: FiniteFloat
    item_orders: list[int] = Field(
        max_length=500, description="The items this adjustment applies to"
    )
    percentage: FiniteFloat | None = Field(default=None)
    eligible_subtotal: FiniteFloat | None = Field(default=None)


class ReceiptCorrectionTaxLineIn(Schema):
    rate: FiniteFloat | None = Field(default=None)
    taxable_amount: FiniteFloat | None = Field(default=None)
    tax_amount: FiniteFloat | None = Field(default=None)


class ReceiptCorrectionIn(Schema):
    decimal_places: int
    receipt_items: list[ReceiptCorrectionItemIn] = Field(max_length=500)
    adjustments: list[ReceiptCorrectionAdjustmentIn] = Field(max_length=100)
    tax_lines: list[ReceiptCorrectionTaxLineIn] = Field(
        default_factory=list, max_length=20
    )
    total_amount: FiniteFloat = Field(description="The total_amount the scan returned")


class ReceiptAllocationItemOut(Schema):
    item_order: int
    list_price: float
    cost: float
    discount: float


class ReceiptAllocationOut(Schema):
    receipt_items: list[ReceiptAllocationItemOut]
    adjustments: list[ReceiptAdjustmentOut]
    reconciliation: ReconciliationOut
    tax_percentage: float
    total_amount: float
