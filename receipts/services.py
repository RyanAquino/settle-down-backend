"""Recalculating corrected receipts, and the response helpers both routes share.

A transcription, or a person's corrections, runs through the pure allocation in
``receipts.allocation``, and these helpers map the result onto the public
schemas: ``cost`` is what each line cost after every adjustment. A receipt that
does not reconcile allocates nothing: every cost is 0 and the review step reads
``list_price``.
"""

from receipts.allocation import (
    Allocation,
    PrintedReceipt,
    allocate,
    corrected_receipt,
    from_minor_units,
)
from receipts.schemas import (
    ReceiptAdjustmentOut,
    ReceiptAllocationItemOut,
    ReceiptAllocationOut,
    ReceiptAllocationShareOut,
    ReceiptCorrectionIn,
    ReconciliationOut,
)


class ReceiptCorrectionError(Exception):
    """A person's choice of items cannot be applied to the receipt."""


def recalculate_receipt(payload: ReceiptCorrectionIn) -> ReceiptAllocationOut:
    """Recalculate a scanned receipt with the items a person chose per adjustment.

    Raises ``ReceiptCorrectionError`` when a choice cannot be applied (an
    unknown item, or a discount larger than its items). Only the allocation is
    guarded, so a fault while building the response is not mistaken for one.
    """
    try:
        receipt, allocation = _corrected_allocation(payload)
    except ValueError as error:
        raise ReceiptCorrectionError(str(error)) from error
    return _allocation_response(receipt, allocation)


def item_costs(
    receipt: PrintedReceipt, allocation: Allocation
) -> list[tuple[float, float]]:
    """Each item's (cost, discount): what it cost and what discounts took off it.

    The discount is the item's share of every discount and points line, so it
    stays exact when tax or a surcharge was spread into the cost.
    """
    places = receipt.decimal_places
    reconciled = allocation.status == "reconciled"
    saved = dict.fromkeys(allocation.costs, 0)
    if reconciled:
        for entry in allocation.adjustments:
            if entry.adjustment.kind != "surcharge":
                for order, share in entry.allocations.items():
                    saved[order] += share
    return [
        (
            from_minor_units(allocation.costs[item.item_order], places),
            from_minor_units(saved[item.item_order], places),
        )
        for item in receipt.items
    ]


def adjustments_out(
    receipt: PrintedReceipt, allocation: Allocation
) -> list[ReceiptAdjustmentOut]:
    places = receipt.decimal_places
    reconciled = allocation.status == "reconciled"
    return [
        ReceiptAdjustmentOut(
            printed_label=entry.adjustment.label,
            kind=entry.adjustment.kind,
            amount=from_minor_units(entry.adjustment.amount, places),
            percentage=(
                None
                if entry.adjustment.percentage is None
                else float(entry.adjustment.percentage)
            ),
            eligible_subtotal=(
                None
                if entry.adjustment.eligible_subtotal is None
                else from_minor_units(entry.adjustment.eligible_subtotal, places)
            ),
            item_orders=list(entry.item_orders),
            status=entry.status,
            # Shares only mean something once the whole receipt reconciles.
            allocations=(
                [
                    ReceiptAllocationShareOut(
                        item_order=order, amount=from_minor_units(share, places)
                    )
                    for order, share in entry.allocations.items()
                ]
                if reconciled
                else []
            ),
        )
        for entry in allocation.adjustments
    ]


def reconciliation_out(
    receipt: PrintedReceipt, allocation: Allocation
) -> ReconciliationOut:
    return ReconciliationOut(
        status=allocation.status,
        difference=from_minor_units(allocation.difference, receipt.decimal_places),
    )


def _corrected_allocation(
    payload: ReceiptCorrectionIn,
) -> tuple[PrintedReceipt, Allocation]:
    """Allocate a scanned receipt with the items a person chose per adjustment.

    No model call and no image: the same allocation as the scan, with each
    adjustment applied to the items given. Raises ``ValueError`` when a choice
    cannot be applied (an unknown item, or a discount larger than its items).
    """
    receipt = corrected_receipt(
        decimal_places=payload.decimal_places,
        items=[(item.item_order, item.list_price) for item in payload.receipt_items],
        adjustments=[
            (
                adjustment.printed_label,
                adjustment.kind,
                adjustment.amount,
                adjustment.percentage,
                adjustment.eligible_subtotal,
            )
            for adjustment in payload.adjustments
        ],
        tax_lines=[
            (line.rate, line.taxable_amount, line.tax_amount)
            for line in payload.tax_lines
        ],
        amount_due=payload.total_amount,
    )
    allocation = allocate(
        receipt,
        scopes=[tuple(adjustment.item_orders) for adjustment in payload.adjustments],
    )
    return receipt, allocation


def _allocation_response(
    receipt: PrintedReceipt, allocation: Allocation
) -> ReceiptAllocationOut:
    """Map a corrected allocation onto the public recalculation response."""
    return ReceiptAllocationOut(
        receipt_items=[
            ReceiptAllocationItemOut(
                item_order=item.item_order,
                list_price=from_minor_units(item.price, receipt.decimal_places),
                cost=cost,
                discount=discount,
            )
            for item, (cost, discount) in zip(
                receipt.items, item_costs(receipt, allocation), strict=True
            )
        ],
        adjustments=adjustments_out(receipt, allocation),
        reconciliation=reconciliation_out(receipt, allocation),
        tax_percentage=float(allocation.tax_rate or 0),
        total_amount=from_minor_units(allocation.total, receipt.decimal_places),
    )
