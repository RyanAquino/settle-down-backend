import pytest
from pydantic_ai import ModelRetry

from backend_api.dataclasses.receipt_item import ReceiptData, ReceiptItemData
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
