"""Printed evidence requested from the OCR model."""

from typing import Literal

from pydantic import Field, FiniteFloat

from backend_api.dto.receipt_item import ReceiptFields, SplitDeduction


class ScannedReceiptAdjustment(SplitDeduction):
    kind: Literal["discount", "points_redemption"]
    printed_label: str = Field(
        default="",
        description="Copy the original printed label of this basket discount or points redemption",
    )
    eligible_subtotal: FiniteFloat | None = Field(
        default=None,
        gt=0,
        description="Printed amount eligible for this offer (e.g. 全品割引対象金額), before the deduction. Null if absent.",
    )


class ScannedReceipt(ReceiptFields):
    receipt_adjustments: list[ScannedReceiptAdjustment] = Field(
        default_factory=list,
        description="Only purchase-wide coupons and redeemed points. Exclude every 1品割引 or item-specific saving.",
    )
