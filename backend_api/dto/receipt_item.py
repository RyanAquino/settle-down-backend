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
        description="Line total for the quantity shown, after item-specific discounts and before basket coupons and points"
    )
    quantity: int = Field(description="The quantity of the purchased item")
    discount: FiniteFloat = Field(
        ge=0,
        default=0,
        description="The discount amount applied to this line, in the receipt's currency (0 if none). Already subtracted from cost",
    )


class ReceiptFields(BaseModel):
    receipt_items: list[ReceiptItemData] = Field(
        description="The list of purchased items in the receipt"
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


class ReceiptData(ReceiptFields):
    receipt_discount: FiniteFloat = Field(
        default=0,
        ge=0,
        description="Combined value of checkout coupons, excluding savings already in item costs",
    )
    points_redeemed: FiniteFloat = Field(
        default=0,
        ge=0,
        description="Monetary value of loyalty points spent on this receipt",
    )


class DeductionEvidence(BaseModel):
    amount: FiniteFloat = Field(
        gt=0, description="Positive monetary value deducted once across the receipt"
    )
    item_orders: list[int] = Field(
        default_factory=list,
        description="Purchased line references explicitly identified by the receipt",
    )

    printed_label: str = Field(
        default="", description="Original printed deduction label"
    )
    eligible_subtotal: FiniteFloat | None = Field(
        default=None,
        gt=0,
        description="Printed subtotal eligible for this offer, before the deduction",
    )


DeductionScope = Literal["basket", "items", "needs_review"]


class SplitDeduction(DeductionEvidence):
    scope: DeductionScope = Field(
        description="Apply across the basket, to the referenced lines, or ask the shopper to confirm eligibility"
    )


class ReceiptSplitRules(BaseModel):
    """Deductions for allocating each receipt total in printed order."""

    discounts: list[SplitDeduction] = Field(default_factory=list)
    points: list[SplitDeduction] = Field(default_factory=list)
