"""How a filed receipt is split between members: pure money math.

``compute_member_totals`` and ``compute_weights`` are called by
``settle_up.services.SettleUpClient`` and kept apart from it so they can be
unit-tested in isolation: callers pass ``members`` in as data.

This module must stay free of web-framework, HTTP and cache imports (enforced by
import-linter), so its inputs are described structurally (``MemberCost``) rather
than by importing the API schemas that happen to satisfy them.
"""

import math
from collections import defaultdict
from collections.abc import Sequence
from functools import reduce
from typing import Protocol

from settle_up.dto import SettleUpMember


class MemberCost(Protocol):
    """One itemized cost charged to one member (e.g. ``UserTransactionSchema``)."""

    @property
    def member_id(self) -> str:
        """The Settle Up member the cost is charged to."""

    @property
    def cost(self) -> float:
        """The line cost, in the group's currency."""


def compute_weights(shares: Sequence[float]) -> list[int]:
    """Convert member shares into the smallest integer-ratio weights.

    shares: list of member shares (e.g., [36, 64])
    returns: list of weights (e.g., [9, 16])
    """
    scaled = [round(s * 100) for s in shares]
    gcd_all = reduce(math.gcd, scaled)
    return [s // gcd_all for s in scaled]


def compute_member_totals(
    receipt_items: Sequence[MemberCost],
    tax_percentage: float,
    members: Sequence[SettleUpMember],
    total_amount: float,
    split_receipt_items: Sequence[float] | None = None,
) -> dict[str, float]:
    """Map each member to the amount they owe, in the group's currency.

    Infers whether the printed total already includes consumption tax via an
    exact float `==` comparison (load-bearing - do not change). The round(_, 2)
    on every tax term is intentional.
    """
    if split_receipt_items is None:
        split_receipt_items = []

    member_receipt_item_total_map: defaultdict[str, float] = defaultdict(float)
    tax_rate = tax_percentage / 100

    for item in receipt_items:
        member_receipt_item_total_map[item.member_id] += item.cost

    member_receipt_tax_map = {
        member_id: round(item_amt * tax_rate, 2)
        for member_id, item_amt in member_receipt_item_total_map.items()
    }

    shared_tax = 0.0
    if split_receipt_items:
        for total_amt in split_receipt_items:
            shared_tax += total_amt + round(total_amt * tax_rate, 2)

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
        for member_id in member_receipt_item_total_map:
            member_receipt_item_total_map[member_id] += member_receipt_tax_map[
                member_id
            ]

    for shared_item in split_receipt_items:
        for member in members:
            member_id = member["id"]
            portion_amt = shared_item / len(members)
            member_receipt_item_total_map[member_id] += portion_amt

            if should_compute_tax:
                member_receipt_item_total_map[member_id] += round(
                    portion_amt * tax_rate, 2
                )

    return member_receipt_item_total_map
