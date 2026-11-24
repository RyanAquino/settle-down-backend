"""Allocation of transcribed receipts: golden vectors plus the edges around them.

``fixtures/allocation-vectors.json`` holds whole receipts, as the OCR model
would transcribe them, with the expected result worked out by hand. Each vector
runs through conversion, scope resolution, allocation and reconciliation, and
its result is checked against a restatement of the installed app's own check.
"""

import time
from fractions import Fraction

import pytest

from receipts.allocation import (
    MAX_ITEMS,
    Adjustment,
    Item,
    PrintedReceipt,
    TaxLine,
    TranscriptionError,
    allocate,
    allocate_scan,
    corrected_receipt,
    decimal_places_for,
    from_minor_units,
    printed_receipt,
    split_proportionally,
    to_minor_units,
)
from receipts.tests import client_gate, scans


@pytest.mark.parametrize(
    "vector", scans.VECTORS, ids=[v["name"] for v in scans.VECTORS]
)
class TestGoldenVectors:
    def test_allocation_matches_the_worked_result(self, vector):
        expected = vector["expected"]
        receipt, result = allocate_scan(scans.scan(vector))
        places = receipt.decimal_places

        assert result.status == expected["status"]
        assert [from_minor_units(cost, places) for cost in result.costs.values()] == (
            expected["costs"]
        )
        assert from_minor_units(result.total, places) == expected["total"]
        assert float(result.tax_rate or 0) == expected["tax_percentage"]
        assert result.tax_added == expected["tax_added"]
        assert from_minor_units(result.difference, places) == expected["difference"]
        assert [
            [entry.status, list(entry.item_orders)] for entry in result.adjustments
        ] == expected["scopes"]
        if "allocations" in expected:
            assert [
                {str(order): share for order, share in entry.allocations.items()}
                for entry in result.adjustments
            ] == expected["allocations"]

    def test_reconciled_receipts_add_up_and_balance_every_adjustment(self, vector):
        receipt, result = allocate_scan(scans.scan(vector))
        if result.status != "reconciled":
            return
        moved = dict.fromkeys(result.costs, 0)
        for entry in result.adjustments:
            assert sum(entry.allocations.values()) == entry.adjustment.amount
            assert set(entry.allocations) <= set(entry.item_orders)
            sign = 1 if entry.adjustment.kind == "surcharge" else -1
            for order, share in entry.allocations.items():
                moved[order] += sign * share
        prices = {item.item_order: item.price for item in receipt.items}
        # Beyond the adjustments, a cost only ever carries a share of printed
        # tax that the client could not add on top itself.
        spread_tax = {
            order: result.costs[order] - prices[order] - moved[order]
            for order in prices
        }
        assert all(share >= 0 for share in spread_tax.values())
        assert sum(spread_tax.values()) in (
            0,
            sum(line.tax_amount or 0 for line in receipt.tax_lines),
            sum(
                line.tax_amount or 0
                for line in receipt.tax_lines
                if line.rate is not None
            ),
        )
        assert all(cost >= 0 for cost in result.costs.values())
        if not result.tax_added:
            assert sum(result.costs.values()) == result.total

    def test_the_installed_app_accepts_exactly_the_reconciled_receipts(self, vector):
        receipt, result = allocate_scan(scans.scan(vector))
        places = receipt.decimal_places
        accepted = client_gate.accepts(
            costs=[from_minor_units(cost, places) for cost in result.costs.values()],
            tax_percentage=float(result.tax_rate or 0),
            total=from_minor_units(result.total, places),
            item_orders=list(result.costs),
        )
        if result.status == "reconciled":
            assert accepted
        elif receipt.adjustments:
            # Fail closed: a receipt the server could not reconcile must not
            # look reconciled to an app that cannot show why.
            assert not accepted


class TestSplitProportionally:
    def test_exact_shares_are_kept(self):
        assert split_proportionally(1735, {1: 12000, 2: 4800, 3: 550}) == {
            1: 1200,
            2: 480,
            3: 55,
        }

    def test_the_largest_remainders_take_the_leftover_units(self):
        # Floors 121, 108, 263 leave 2 units; remainders 3514, 1434, 2408 of 3678.
        assert split_proportionally(494, {1: 908, 2: 807, 3: 1963}) == {
            1: 122,
            2: 108,
            3: 264,
        }

    def test_ties_go_to_the_lower_item(self):
        assert split_proportionally(101, {1: 500, 2: 500}) == {1: 51, 2: 50}
        assert split_proportionally(1, {2: 10, 1: 10, 3: 10}) == {2: 0, 1: 1, 3: 0}

    def test_remainders_are_compared_exactly(self):
        # Float remainders rank 2 over [1, 3, 10] as [0, 0, 2]; exactly,
        # items 2 and 3 tie at 6/14 and the lower item wins the second unit.
        assert split_proportionally(2, {1: 1, 2: 3, 3: 10}) == {1: 0, 2: 1, 3: 1}

    def test_zero_weights_take_nothing(self):
        assert split_proportionally(19, {1: 0, 2: 380}) == {1: 0, 2: 19}


class TestMinorUnits:
    def test_round_trip(self):
        assert to_minor_units(12.99, 2) == 1299
        assert to_minor_units(5809, 0) == 5809
        assert from_minor_units(1299, 2) == 12.99
        assert from_minor_units(5809, 0) == 5809.0

    def test_an_amount_beyond_any_receipt_is_rejected(self):
        # Keeps every sum a finite float in the response.
        with pytest.raises(TranscriptionError, match="larger than any receipt"):
            to_minor_units(1e13, 0)

    def test_an_amount_finer_than_the_currency_is_rejected(self):
        with pytest.raises(TranscriptionError, match="more than 0 decimal places"):
            to_minor_units(12.5, 0)

    def test_decimal_places_follow_the_printed_amounts(self):
        assert decimal_places_for(0, [12.99, 3.0]) == 2
        assert decimal_places_for(2, [10.0, 20.0]) == 2
        assert decimal_places_for(0, [5809.0]) == 0
        assert decimal_places_for(-1, [100.0]) == 0

    @pytest.mark.parametrize(
        ("declared", "amounts"), [(4, [1.0]), (0, [1.2345]), (-1, [-1.0])]
    )
    def test_impossible_decimal_places_are_a_transcription_error(
        self, declared, amounts
    ):
        if declared == -1:
            # A negative count is repaired by the amounts when they are whole.
            assert decimal_places_for(declared, amounts) == 0
            return
        with pytest.raises(TranscriptionError, match="decimal_places"):
            decimal_places_for(declared, amounts)


class TestTranscriptionErrors:
    def _vector(self, **changes):
        return scans.scan({**scans.vector("sugi"), **changes})

    def test_no_items(self):
        with pytest.raises(TranscriptionError, match="No purchased items"):
            printed_receipt(self._vector(items=[]))

    def test_negative_price(self):
        with pytest.raises(TranscriptionError, match="negative price"):
            printed_receipt(self._vector(items=[987, -347, 2310, 1958, 767, 278]))

    def test_more_lines_than_any_receipt(self):
        with pytest.raises(TranscriptionError, match="Too many lines"):
            printed_receipt(self._vector(items=[1] * (MAX_ITEMS + 1)))

    @pytest.mark.parametrize("amount_due", [0, -5809])
    def test_non_positive_amount_due(self, amount_due):
        with pytest.raises(TranscriptionError, match="amount_due"):
            printed_receipt(self._vector(amount_due=amount_due))


def _receipt(prices, adjustments, amount_due, tax_lines=()):
    return PrintedReceipt(
        items=tuple(
            Item(item_order=order, price=price)
            for order, price in enumerate(prices, start=1)
        ),
        adjustments=tuple(adjustments),
        tax_lines=tuple(tax_lines),
        amount_due=amount_due,
        decimal_places=0,
    )


class TestChosenScopes:
    """A person picks the items for each adjustment; the math is the same."""

    COUPON = Adjustment(label="クーポン", kind="discount", amount=150)

    def test_chosen_scopes_are_allocated_as_given(self):
        result = allocate(_receipt([108, 540], [self.COUPON], 498), scopes=[(1, 2)])

        assert result.status == "reconciled"
        assert result.costs == {1: 83, 2: 415}
        (entry,) = result.adjustments
        assert (entry.status, entry.item_orders) == ("verified", (1, 2))

    def test_a_scope_naming_an_item_twice_counts_it_once(self):
        result = allocate(_receipt([108, 540], [self.COUPON], 498), scopes=[(2, 2)])

        assert result.costs == {1: 108, 2: 390}

    def test_one_scope_per_adjustment(self):
        with pytest.raises(ValueError, match="exactly one scope"):
            allocate(_receipt([108, 540], [self.COUPON], 498), scopes=[])

    @pytest.mark.parametrize("scope", [(), (3,), (1, 9)])
    def test_a_scope_must_name_items_on_the_receipt(self, scope):
        with pytest.raises(ValueError, match="must apply to items"):
            allocate(_receipt([108, 540], [self.COUPON], 498), scopes=[scope])

    def test_a_discount_must_fit_its_items(self):
        with pytest.raises(ValueError, match="larger than what its items"):
            allocate(_receipt([108, 540], [self.COUPON], 498), scopes=[(1,)])

    def test_a_surcharge_needs_items_that_still_cost_something(self):
        free = Adjustment(label="無料", kind="discount", amount=108)
        fee = Adjustment(label="手数料", kind="surcharge", amount=500)
        with pytest.raises(ValueError, match="larger than what its items"):
            allocate(_receipt([108, 540], [free, fee], 1040), scopes=[(1,), (1,)])

        result = allocate(_receipt([108, 540], [free, fee], 1040), scopes=[(1,), (2,)])
        assert result.status == "reconciled"
        assert result.costs == {1: 0, 2: 1040}


class TestScopeEdges:
    def test_an_adjustment_on_a_receipt_with_nothing_left_is_unresolved(self):
        free = Adjustment(label="無料", kind="discount", amount=100)
        fee = Adjustment(label="手数料", kind="surcharge", amount=10)

        result = allocate(_receipt([100], [free, fee], 10))

        assert result.status == "needs_review"
        assert [entry.status for entry in result.adjustments] == [
            "verified",
            "unresolved",
        ]

    def test_a_unique_base_is_found_among_many_items(self):
        # Powers of two reach each sum one way only.
        prices = [2**bit for bit in range(16)]
        discount = Adjustment(
            label="対象商品割引", kind="discount", amount=1, eligible_subtotal=40_000
        )

        result = allocate(_receipt(prices, [discount], sum(prices) - 1))

        assert result.status == "reconciled"
        (entry,) = result.adjustments
        assert {prices[order - 1] for order in entry.item_orders} == {
            2**bit for bit in range(16) if 40_000 >> bit & 1
        }

    def test_the_subset_search_gives_up_once_its_work_runs_out(self):
        # Every sum up to 40,000 is reachable many ways: about 16 million steps.
        prices = list(range(1, 401))
        discount = Adjustment(
            label="対象商品割引", kind="discount", amount=1, eligible_subtotal=40_000
        )

        started = time.monotonic()
        result = allocate(_receipt(prices, [discount], sum(prices) - 1))

        assert time.monotonic() - started < 5
        assert result.status == "needs_review"
        assert [entry.status for entry in result.adjustments] == ["unresolved"]

    def test_a_base_larger_than_every_item_together_is_unresolved(self):
        discount = Adjustment(
            label="割引", kind="discount", amount=10, eligible_subtotal=5000
        )

        result = allocate(_receipt([1000, 2000], [discount], 2990))

        assert [entry.status for entry in result.adjustments] == ["unresolved"]

    def test_tax_rates_are_exact_fractions(self):
        tax = TaxLine(rate=Fraction(33, 4), taxable_amount=None, tax_amount=None)

        result = allocate(_receipt([1000], [], 1083, [tax]))

        assert result.status == "reconciled"
        assert result.tax_added
        assert result.tax_rate == Fraction(33, 4)


class TestCorrectedReceipt:
    def test_more_lines_than_any_receipt_are_refused(self):
        with pytest.raises(ValueError, match="Too many lines"):
            corrected_receipt(
                decimal_places=0,
                items=[(order, 100) for order in range(1, MAX_ITEMS + 2)],
                adjustments=[],
                tax_lines=[],
                amount_due=100,
            )
