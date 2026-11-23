from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelRetry

from backend_api.dto.receipt_item import (
    ReceiptData,
    ReceiptItemData,
    ScannedReceiptAdjustment,
)
from backend_api.ocr import validate_receipt_data


def _item(cost, english_name="Coffee"):
    return ReceiptItemData(
        english_name=english_name,
        japanese_name="コーヒー",
        item_order=1,
        cost=cost,
        quantity=1,
    )


def _receipt(total_amount, items):
    return ReceiptData(
        en_shop_name="Cafe",
        jp_shop_name="カフェ",
        tax_percentage=10,
        total_amount=total_amount,
        receipt_items=items,
    )


class TestValidateReceiptData:
    """The OCR output validator guards structure only (not amount reconciliation)."""

    def test_valid_receipt_passes_through_unchanged(self):
        data = _receipt(500, [_item(500)])
        assert validate_receipt_data(data) is data

    def test_non_positive_total_raises(self):
        with pytest.raises(ModelRetry):
            validate_receipt_data(_receipt(0, [_item(500)]))

    def test_empty_items_raises(self):
        with pytest.raises(ModelRetry):
            validate_receipt_data(_receipt(500, []))

    def test_negative_item_cost_raises(self):
        with pytest.raises(ModelRetry):
            validate_receipt_data(_receipt(500, [_item(-1)]))


@pytest.mark.parametrize("orders", [[2], [1, 1]])
def test_adjustment_scope_requires_unique_purchased_lines(orders):
    data = _receipt(300, [_item(500)])
    data.receipt_adjustments = [
        ScannedReceiptAdjustment(kind="discount", amount=200, item_orders=orders)
    ]
    with pytest.raises(ModelRetry, match="eligible item_order"):
        validate_receipt_data(data)


def test_points_can_cover_the_entire_printed_total():
    data = _receipt(0, [_item(500)])
    data.receipt_adjustments = [
        ScannedReceiptAdjustment(kind="points_redemption", amount=500, item_orders=[1])
    ]
    assert validate_receipt_data(data) is data


@pytest.mark.parametrize("amount", [0, -2, float("inf")])
def test_deductions_must_be_positive_finite_currency_amounts(amount):
    with pytest.raises(ValidationError):
        ScannedReceiptAdjustment(kind="discount", amount=amount)


def test_item_coupon_misread_as_a_basket_deduction_retries_instead_of_charging_the_wrong_shopper():
    data = _receipt(5809, [_item(987), _item(2310)])
    data.receipt_adjustments = [
        ScannedReceiptAdjustment(
            kind="discount", amount=347, item_orders=[1], printed_label="１ 品割引"
        )
    ]
    with pytest.raises(ModelRetry, match="immediately preceding"):
        validate_receipt_data(data)


def test_printed_coupon_base_recovers_only_the_four_eligible_sugi_lines():
    fixture = Path(__file__).parent / "fixtures" / "sugi-receipt.json"
    data = ReceiptData.model_validate_json(fixture.read_bytes())
    coupon = data.receipt_adjustments[0]
    coupon.item_orders = []
    coupon.eligible_subtotal = 2909
    assert validate_receipt_data(data).receipt_adjustments[0].item_orders == [
        1,
        2,
        5,
        6,
    ]


@pytest.mark.parametrize(
    ("prices", "target", "marked", "expected"),
    [
        ([0, 200, 5.05, 2.95], 8, [2], [3, 4]),
        ([10, 10], 10, [], []),
        ([10, 10], 10, [2], [2]),
        ([3, 4], 8, [], []),
        ([2**i for i in range(15)], 50_000, [], []),
    ],
)
def test_coupon_base_preserves_explicit_marks_and_leaves_ambiguous_scopes_open(
    prices, target, marked, expected
):
    items = [_item(price) for price in prices]
    for i, item in enumerate(items, 1):
        item.item_order = i
    data = _receipt(1, items)
    data.receipt_adjustments = [
        ScannedReceiptAdjustment(
            kind="discount", amount=1, item_orders=marked, eligible_subtotal=target
        )
    ]
    assert validate_receipt_data(data).receipt_adjustments[0].item_orders == expected


class TestDecimalCurrencies:
    """Amounts are denominated in the group's currency, not always whole yen."""

    def test_fractional_discount_is_preserved(self):
        # HKD/USD receipts carry cents; an int field would silently truncate.
        item = ReceiptItemData(
            english_name="Coffee",
            japanese_name="コーヒー",
            item_order=1,
            cost=42.50,
            quantity=1,
            discount=7.25,
        )

        assert item.discount == 7.25
