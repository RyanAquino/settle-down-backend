from pathlib import Path

import pytest

from backend_api.dto.scanned_receipt import ScannedReceipt, ScannedReceiptAdjustment
from backend_api.ocr import validate_receipt_data
from backend_api.receipt_normalization import normalize_receipt


@pytest.fixture
def scan():
    fixture = Path(__file__).parent / "fixtures" / "sugi-receipt.json"
    return ScannedReceipt.model_validate_json(fixture.read_bytes())


def test_printed_subtotal_identifies_four_eligible_lines_without_changing_the_scan(
    scan,
):
    original = scan.model_dump()
    receipt, rules = normalize_receipt(validate_receipt_data(scan))
    assert scan.model_dump() == original
    assert receipt.receipt_items == scan.receipt_items
    assert receipt.receipt_discount == 233
    assert receipt.points_redeemed == 494
    assert receipt.total_amount == 5809
    assert rules.model_dump() == {
        "discounts": [{"amount": 233, "item_orders": [1, 2, 5, 6]}],
        "points": [],
    }


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
def test_printed_base_preserves_marks_and_only_infers_a_unique_scope(
    scan, prices, target, marked, expected
):
    scan.receipt_items = [
        scan.receipt_items[0].model_copy(update={"cost": price, "item_order": i})
        for i, price in enumerate(prices, 1)
    ]
    scan.receipt_adjustments = [
        ScannedReceiptAdjustment(
            kind="discount", amount=1, item_orders=marked, eligible_subtotal=target
        )
    ]
    receipt, rules = normalize_receipt(scan)
    assert receipt.receipt_discount == 1
    assert [rule.item_orders for rule in rules.discounts] == (
        [expected] if expected else []
    )


def test_multiple_coupons_and_redemptions_keep_printed_order_and_decimal_totals(scan):
    scan.receipt_adjustments = [
        ScannedReceiptAdjustment(kind="discount", amount=0.1),
        ScannedReceiptAdjustment(kind="points_redemption", amount=0.4, item_orders=[2]),
        ScannedReceiptAdjustment(kind="discount", amount=0.2, item_orders=[2, 3]),
        ScannedReceiptAdjustment(kind="points_redemption", amount=0.05),
    ]
    receipt, rules = normalize_receipt(scan)
    assert receipt.receipt_discount == 0.3
    assert receipt.points_redeemed == 0.45
    assert rules.model_dump() == {
        "discounts": [
            {"amount": 0.1, "item_orders": []},
            {"amount": 0.2, "item_orders": [2, 3]},
        ],
        "points": [
            {"amount": 0.4, "item_orders": [2]},
            {"amount": 0.05, "item_orders": []},
        ],
    }
