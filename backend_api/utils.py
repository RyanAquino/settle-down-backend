"""General utility helpers for backend_api.

Currently holds the receipt-splitting math (``compute_member_totals``,
``compute_weights``) extracted from ``SettleUpClient`` so it can be unit-tested
in isolation — these are pure, callers pass ``members`` in as data. May grow to
include other shared helpers (including I/O-touching ones); keep those distinct
from ``settleup_utils.SettleUpClient`` so this module stays cohesive.
"""

import math
from collections import defaultdict
from functools import reduce

from backend_api.dto.receipt_item import ReceiptItemData
from backend_api.schemas import UserTransactionSchema

# Ceiling for per-unit spreading; no plausible group receipt line exceeds it.
MAX_SPREAD_UNITS = 100


def format_amount(value: float) -> str:
    """Render a money amount for the Settle Up API: no trailing .0 on whole values."""
    return str(int(value)) if float(value).is_integer() else str(value)


def split_amount_evenly(amount: float, parts: int) -> list[float]:
    """Split a non-negative ``amount`` into ``parts`` portions summing back to it exactly.

    Splits in whole currency units when ``amount`` is integral (JPY/TWD
    receipts have no subunits) and in hundredths otherwise (HKD cents); the
    first portions absorb the remainder, e.g. 1000/3 -> [334, 333, 333].
    """
    scale = 1 if float(amount).is_integer() else 100
    base, remainder = divmod(round(amount * scale), parts)
    return [
        round((base + (1 if i < remainder else 0)) / scale, 2) for i in range(parts)
    ]


def spread_item_quantities(items: list[ReceiptItemData]) -> list[ReceiptItemData]:
    """Spread each multi-quantity line into one quantity-1 item per unit.

    "Shake x3 / 1050" becomes three quantity-1 "Shake" items (350 each), so
    every unit can be assigned to a different group member. The line's cost
    and discount are distributed with their sums preserved, and item_order is
    renumbered sequentially over the spread list (emission order is kept).

    Lines with quantity above ``MAX_SPREAD_UNITS`` pass through unspread: a
    misread (e.g. barcode digits landing in quantity) must not fan out into
    millions of item copies.
    """
    spread: list[ReceiptItemData] = []
    for item in items:
        if item.quantity <= 1 or item.quantity > MAX_SPREAD_UNITS:
            spread.append(item)
            continue
        costs = split_amount_evenly(item.cost, item.quantity)
        discounts = split_amount_evenly(item.discount, item.quantity)
        spread.extend(
            item.model_copy(update={"quantity": 1, "cost": cost, "discount": discount})
            for cost, discount in zip(costs, discounts)
        )
    for order, item in enumerate(spread, start=1):
        item.item_order = order
    return spread


def compute_weights(shares) -> list[int]:
    """Convert member shares into the smallest integer-ratio weights.

    shares: list of member shares (e.g., [36, 64])
    returns: list of weights (e.g., [9, 16])
    """
    # Step 1: convert shares to integers if they aren't already
    scaled = [int(round(s * 100)) for s in shares]

    # Step 2: find GCD of all shares
    gcd_all = reduce(math.gcd, scaled)

    # Step 3: divide each share by the GCD to get weights
    weights = [s // gcd_all for s in scaled]

    return weights


def compute_member_totals(
    receipt_items: list[UserTransactionSchema],
    tax_percentage: int,
    members: list[dict],
    total_amount: float = 0,
    split_receipt_items: list[float] | None = None,
) -> dict[str, float]:
    """Map each member to the amount they owe, in the group's currency.

    Infers whether the printed total already includes consumption tax via an
    exact float `==` comparison (load-bearing — do not change). The round(_, 2)
    on every tax term is intentional.
    """
    if split_receipt_items is None:
        split_receipt_items = []

    # Calculate tax
    member_receipt_item_total_map = defaultdict(float)
    member_receipt_tax_map = defaultdict(float)
    tax_percentage /= 100

    # Total cost per member
    for member in receipt_items:
        member_receipt_item_total_map[member.member_id] += member.cost

    # Tax per consolidated items member
    for member_id, item_amt in member_receipt_item_total_map.items():
        member_receipt_tax_map[member_id] += round(item_amt * tax_percentage, 2)

    # Shared tax for total verification
    shared_tax = 0
    if split_receipt_items:
        for total_amt in split_receipt_items:
            shared_tax += total_amt + round(total_amt * tax_percentage, 2)

    should_compute_tax = (
        sum(
            [
                *member_receipt_tax_map.values(),
                *member_receipt_item_total_map.values(),
                shared_tax,
            ]
        )
        == total_amount
    )
    if should_compute_tax:
        for member_id in member_receipt_item_total_map.keys():
            member_receipt_item_total_map[member_id] += member_receipt_tax_map.get(
                member_id, 0
            )

    # Shared item split cost + tax if applicable
    for shared_item in split_receipt_items:
        for member in members:
            member_id = member["id"]
            portion_amt = shared_item / len(members)
            member_receipt_item_total_map[member_id] += portion_amt

            if should_compute_tax:
                member_receipt_item_total_map[member_id] += round(
                    portion_amt * tax_percentage, 2
                )

    return member_receipt_item_total_map
