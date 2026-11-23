from datetime import datetime
from typing import Literal

from django.utils import timezone
from pydantic import BaseModel, Field, FiniteFloat


class ReceiptItemData(BaseModel):
    """
    Receipt Item model for each item in the receipt.
    """

    english_name: str = Field(
        description="The name of the purchased item in english in the receipt"
    )
    japanese_name: str = Field(
        description="The name of the purchased item in Japanese in the receipt"
    )
    item_order: int = Field(description="The order of the items in the receipt")
    cost: FiniteFloat = Field(
        description="Line total for the quantity shown, after item-specific discounts and before receipt adjustments"
    )
    quantity: int = Field(description="The quantity of the purchased item")
    discount: float = Field(
        default=0,
        description="The discount amount applied to this line, in the receipt's currency (0 if none). Already subtracted from cost",
    )


class ReceiptAdjustment(BaseModel):
    kind: Literal["discount", "points_redemption"]
    amount: FiniteFloat = Field(
        gt=0, description="Positive monetary value deducted once across the receipt"
    )
    item_orders: list[int] = Field(
        default_factory=list,
        description="Eligible item_order values when identifiable; empty for the whole basket",
    )


class ScannedReceiptAdjustment(ReceiptAdjustment):
    printed_label: str = Field(
        default="",
        description="Copy the original printed label of this basket discount or points redemption",
    )
    eligible_subtotal: FiniteFloat | None = Field(
        default=None,
        gt=0,
        description="Printed amount eligible for this offer (e.g. 全品割引対象金額), before the deduction. Null if absent.",
    )


class ReceiptData(BaseModel):
    receipt_adjustments: list[ScannedReceiptAdjustment] = Field(
        default_factory=list,
        description="Only purchase-wide coupons and redeemed points. Exclude every 1品割引 or item-specific saving.",
    )
    receipt_items: list[ReceiptItemData] = Field(
        default_factory=list, description="The list of items in the receipt"
    )
    en_shop_name: str = Field(
        ..., description="The name of the shop in the receipt in english"
    )
    jp_shop_name: str = Field(
        ..., description="The name of the shop in the receipt in japanese"
    )
    tax_percentage: float = Field(
        default=0,
        description="The single printed tax rate, or 0 for no rate or multiple rates",
    )
    total_amount: float = Field(
        default=0,
        description="Final amount paid after all discounts and redeemed points",
    )
    receipt_date: datetime = Field(
        default_factory=timezone.now, description="The date of the receipt"
    )
