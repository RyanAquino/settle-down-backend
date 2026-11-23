"""Receipt transcriptions for tests, built from the golden allocation vectors.

``fixtures/allocation-vectors.json`` holds whole receipts as the OCR model would
transcribe them, each with its result worked out by hand. ``scan_payload``
turns one into the JSON the model returns, ``scan`` into a validated
``ReceiptScan``.
"""

import json
from pathlib import Path
from typing import Any

from backend_api.dto.receipt_scan import ReceiptScan

VECTORS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "allocation-vectors.json").read_text(
        encoding="utf-8"
    )
)


def vector(name: str) -> dict[str, Any]:
    return next(v for v in VECTORS if v["name"] == name)


def scan_payload(vector: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The JSON the OCR model returns for a vector, with optional field changes."""
    orders = vector.get("items_as_read") or range(1, len(vector["items"]) + 1)
    payload = {
        "jp_shop_name": "テスト薬局",
        "en_shop_name": "Test Pharmacy",
        "receipt_date": "2026-11-23T15:16:00",
        "decimal_places": vector["decimal_places"],
        "items": [
            {
                "item_order": order,
                "japanese_name": f"商品{position}",
                "english_name": f"Item {position}",
                "quantity": 1,
                "printed_price": price,
            }
            for position, (order, price) in enumerate(
                zip(orders, vector["items"], strict=True), start=1
            )
        ],
        "adjustments": [
            {
                "printed_label": adjustment["label"],
                "kind": adjustment["kind"],
                "amount": adjustment["amount"],
                "percentage": adjustment["percentage"],
                "item_orders": adjustment["item_orders"],
                "eligible_subtotal": adjustment["eligible_subtotal"],
            }
            for adjustment in vector["adjustments"]
        ],
        "tax_lines": [
            {"rate": rate, "taxable_amount": base, "tax_amount": tax}
            for rate, base, tax in vector["tax_lines"]
        ],
        "amount_due": vector["amount_due"],
    }
    return {**payload, **changes}


def scan(vector: dict[str, Any], **changes: Any) -> ReceiptScan:
    return ReceiptScan.model_validate(scan_payload(vector, **changes))
