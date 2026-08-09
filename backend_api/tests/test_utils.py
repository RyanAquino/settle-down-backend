"""Tests for the pure receipt helpers in backend_api.utils.

``spread_item_quantities`` powers per-unit assignment: a "Shake x3" line
becomes three quantity-1 items so each unit can go to a different group
member. ``compute_member_totals`` takes ``members`` as plain data, so the
four-member cases below need no client/fixture mocking.
"""

import pytest
from pydantic import ValidationError

from backend_api.dto.receipt_item import ReceiptItemData
from backend_api.schemas import UserTransactionSchema
from backend_api.utils import (
    compute_member_totals,
    compute_weights,
    format_amount,
    split_amount_evenly,
    spread_item_quantities,
)


class TestFormatAmount:
    def test_whole_value_has_no_trailing_decimal(self):
        assert format_amount(200.0) == "200"

    def test_fractional_value_kept_as_is(self):
        assert format_amount(131.9) == "131.9"


def _item(cost, quantity, name="Shake", order=1, discount=0):
    return ReceiptItemData(
        english_name=name,
        japanese_name="シェイク",
        item_order=order,
        cost=cost,
        quantity=quantity,
        discount=discount,
    )


class TestSplitAmountEvenly:
    def test_integral_amount_splits_in_whole_units(self):
        # JPY/TWD have no subunits: 1000/3 must not produce fractional yen.
        assert split_amount_evenly(1000, 3) == [334, 333, 333]

    def test_decimal_amount_splits_in_cents(self):
        assert split_amount_evenly(100.10, 4) == [25.03, 25.03, 25.02, 25.02]

    def test_even_split_has_equal_parts(self):
        assert split_amount_evenly(1050, 3) == [350, 350, 350]

    @pytest.mark.parametrize("amount,parts", [(1000, 3), (99.9, 3), (0, 4), (7, 4)])
    def test_sum_is_always_preserved(self, amount, parts):
        portions = split_amount_evenly(amount, parts)
        assert len(portions) == parts
        assert sum(portions) == pytest.approx(amount)


class TestSpreadItemQuantities:
    def test_x3_becomes_three_x1(self):
        spread = spread_item_quantities([_item(cost=1050, quantity=3)])

        assert len(spread) == 3
        assert all(i.quantity == 1 for i in spread)
        assert all(i.english_name == "Shake" for i in spread)
        assert all(i.japanese_name == "シェイク" for i in spread)
        assert [i.cost for i in spread] == [350, 350, 350]

    def test_uneven_cost_preserves_line_total(self):
        spread = spread_item_quantities([_item(cost=1000, quantity=3)])
        assert [i.cost for i in spread] == [334, 333, 333]
        assert sum(i.cost for i in spread) == 1000

    def test_decimal_cost_spreads_in_cents(self):
        spread = spread_item_quantities([_item(cost=100.10, quantity=2)])
        assert [i.cost for i in spread] == [50.05, 50.05]

    def test_discount_is_spread_too(self):
        spread = spread_item_quantities([_item(cost=900, quantity=3, discount=60)])
        assert [i.discount for i in spread] == [20, 20, 20]
        assert sum(i.cost for i in spread) == 900

    def test_single_quantity_items_pass_through(self):
        items = [_item(cost=500, quantity=1, name="Coffee")]
        spread = spread_item_quantities(items)
        assert len(spread) == 1
        assert spread[0].cost == 500
        assert spread[0].quantity == 1

    def test_implausible_quantity_passes_through_unspread(self):
        # A barcode misread in quantity must not fan out into item copies.
        items = [_item(cost=500, quantity=4901234)]
        spread = spread_item_quantities(items)
        assert len(spread) == 1
        assert spread[0].quantity == 4901234
        assert spread[0].cost == 500

    def test_zero_quantity_rejected_by_schema(self):
        with pytest.raises(ValidationError):
            _item(cost=500, quantity=0)

    def test_item_orders_renumbered_sequentially(self):
        spread = spread_item_quantities(
            [
                _item(cost=500, quantity=1, name="Coffee", order=1),
                _item(cost=700, quantity=2, name="Shake", order=2),
                _item(cost=300, quantity=1, name="Tea", order=3),
            ]
        )
        assert [i.item_order for i in spread] == [1, 2, 3, 4]
        assert [i.english_name for i in spread] == ["Coffee", "Shake", "Shake", "Tea"]


class TestFourMemberSplit:
    """The split math takes the member list as data — prove 4-way works."""

    MEMBERS = [{"id": f"member_{n}", "name": f"Member {n}"} for n in range(1, 5)]

    def test_four_members_each_claim_one_spread_unit(self):
        # Shake x4 for 1400 spread into four 350 units, one per member,
        # plus a shared 200 appetizer; tax-exclusive 10% receipt.
        units = spread_item_quantities([_item(cost=1400, quantity=4)])
        receipt_items = [
            UserTransactionSchema(member_id=member["id"], cost=unit.cost)
            for member, unit in zip(self.MEMBERS, units)
        ]

        totals = compute_member_totals(
            receipt_items=receipt_items,
            tax_percentage=10,
            members=self.MEMBERS,
            total_amount=1760,  # (1400 + 200) * 1.1
            split_receipt_items=[200],
        )

        assert totals == {m["id"]: 440.0 for m in self.MEMBERS}
        assert sum(totals.values()) == 1760

    def test_four_member_uneven_split_weights(self):
        receipt_items = [
            UserTransactionSchema(member_id="member_1", cost=100),
            UserTransactionSchema(member_id="member_2", cost=200),
            UserTransactionSchema(member_id="member_3", cost=300),
            UserTransactionSchema(member_id="member_4", cost=400),
        ]

        totals = compute_member_totals(
            receipt_items=receipt_items,
            tax_percentage=0,
            members=self.MEMBERS,
            total_amount=1000,
        )

        assert sum(totals.values()) == 1000
        assert compute_weights(tuple(totals.values())) == [1, 2, 3, 4]
