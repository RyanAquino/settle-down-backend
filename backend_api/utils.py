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

from backend_api.schemas import UserTransactionSchema


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
    """Map each member to the yen they owe.

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
