"""Printed evidence requested from the OCR model."""

from typing import Literal

from pydantic import Field

from backend_api.dto.receipt_item import DeductionEvidence, ReceiptFields


class ScannedReceiptAdjustment(DeductionEvidence):
    kind: Literal["discount", "points_redemption"]
    whole_basket: bool = Field(
        default=False,
        description="True when this deduction clearly covers the entire basket, including unrestricted points spent at checkout. False if restricted or unclear.",
    )


class ScannedReceipt(ReceiptFields):
    receipt_adjustments: list[ScannedReceiptAdjustment] = Field(
        default_factory=list,
        description="Only purchase-wide coupons and redeemed points. Exclude every 1品割引 or item-specific saving.",
    )
