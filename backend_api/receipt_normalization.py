"""Convert OCR evidence into receipt totals and editable split rules."""

from decimal import Decimal

from backend_api.dto.receipt_item import (
    ReceiptData,
    ReceiptItemData,
    ReceiptSplitRules,
    SplitDeduction,
)
from backend_api.dto.scanned_receipt import ScannedReceipt, ScannedReceiptAdjustment


def _unique_eligible_items(
    items: list[ReceiptItemData], eligible_subtotal: float
) -> list[int]:
    """Recover missing coupon scope only when the printed base has exactly one match."""
    target = Decimal(str(eligible_subtotal))
    # A bit per purchased line identifies a subset; None records multiple matches.
    subsets: dict[Decimal, int | None] = {Decimal(0): 0}
    for index, item in enumerate(items):
        cost = Decimal(str(item.cost))
        if cost <= 0 or cost > target:
            continue
        for subtotal, mask in list(subsets.items()):
            value = subtotal + cost
            if value <= target:
                subsets[value] = (
                    None if value in subsets or mask is None else mask | (1 << index)
                )
        # A partial basket can already prove ambiguity. Bound the work for long
        # receipts with many distinct decimal sums; an uncertain scope stays empty.
        if subsets.get(target, 0) is None or len(subsets) > 8192:
            return []
    match = subsets.get(target) or 0
    return [item.item_order for i, item in enumerate(items) if match & (1 << i)]


def _split_deduction(
    adjustment: ScannedReceiptAdjustment, items: list[ReceiptItemData]
) -> SplitDeduction:
    orders = adjustment.item_orders
    if adjustment.eligible_subtotal is not None:
        marked = set(orders)
        base = sum(
            Decimal(str(item.cost)) for item in items if item.item_order in marked
        )
        if base != Decimal(str(adjustment.eligible_subtotal)):
            orders = _unique_eligible_items(items, adjustment.eligible_subtotal)
    return SplitDeduction(amount=adjustment.amount, item_orders=orders)


def _total_and_rules(
    deductions: list[SplitDeduction],
) -> tuple[float, list[SplitDeduction]]:
    total = sum(Decimal(str(entry.amount)) for entry in deductions)
    # One basket-wide deduction is fully described by its receipt total.
    if len(deductions) == 1 and not deductions[0].item_orders:
        return float(total), []
    return float(total), deductions


def normalize_receipt(scan: ScannedReceipt) -> tuple[ReceiptData, ReceiptSplitRules]:
    """Preserve item prices and printed order within each stage of the split."""
    discounts: list[SplitDeduction] = []
    points: list[SplitDeduction] = []
    for adjustment in scan.receipt_adjustments:
        rules = discounts if adjustment.kind == "discount" else points
        rules.append(_split_deduction(adjustment, scan.receipt_items))
    receipt_discount, discount_rules = _total_and_rules(discounts)
    points_redeemed, point_rules = _total_and_rules(points)
    receipt = ReceiptData(
        **scan.model_dump(exclude={"receipt_adjustments"}),
        receipt_discount=receipt_discount,
        points_redeemed=points_redeemed,
    )
    return receipt, ReceiptSplitRules(discounts=discount_rules, points=point_rules)
