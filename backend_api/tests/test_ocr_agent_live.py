"""Live OpenRouter test for the OCR agent.

This makes a REAL network call to OpenRouter (and costs credits). Its purpose is
to confirm the Gemini-via-OpenRouter path actually returns valid
strict-structured output — i.e. ``ToolOutput(ScannedReceipt, strict=True)`` plus
image input round-trips through ``settings.OPENROUTER_MODEL``, which a mocked
unit test cannot verify.

It is skipped unless BOTH are present, so the normal suite (and CI, which has no
key) stays green:

  - ``OPENROUTER_API_KEY`` — a real key (``settings`` reads it from env/.env)
  - a receipt image at ``$LIVE_RECEIPT_IMAGE`` (falls back to
    ``backend_api/tests/fixtures/receipt.<jpg|jpeg|png>``)

Run it explicitly with::

    OPENROUTER_API_KEY=... LIVE_RECEIPT_IMAGE=/path/to/receipt.jpg uv run pytest -m live
"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from pydantic_ai import BinaryContent

from backend_api.dto.scanned_receipt import ScannedReceipt
from backend_api.ocr import get_openrouter_receipt_agent

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _receipt_image() -> Path | None:
    """Locate a receipt image: $LIVE_RECEIPT_IMAGE, else a default fixture."""
    env_path = os.getenv("LIVE_RECEIPT_IMAGE")
    if env_path:
        candidate = Path(env_path)
        return candidate if candidate.is_file() else None
    for suffix in _MEDIA_TYPES:
        candidate = _FIXTURE_DIR / f"receipt{suffix}"
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.live
@pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"),
    reason="live test: set OPENROUTER_API_KEY to run",
)
def test_openrouter_live_extracts_valid_receipt():
    image_path = _receipt_image()
    if image_path is None:
        pytest.skip(
            "live test: no receipt image (set LIVE_RECEIPT_IMAGE or add "
            "backend_api/tests/fixtures/receipt.jpg)"
        )

    get_openrouter_receipt_agent.cache_clear()
    agent = get_openrouter_receipt_agent()
    media_type = _MEDIA_TYPES.get(image_path.suffix.lower(), "image/jpeg")

    async def _run():
        return await agent.run(
            [
                "here is the image receipt:",
                BinaryContent(data=image_path.read_bytes(), media_type=media_type),
            ]
        )

    result = asyncio.run(_run())
    data = result.output

    # If agent.run() returned at all, the strict ToolOutput schema was honored
    # by Gemini-via-OpenRouter and the output_validator passed. Assert the
    # extraction is structurally sane.
    assert isinstance(data, ScannedReceipt)
    assert data.total_amount > 0
    assert data.receipt_items, "expected at least one extracted line item"


@pytest.mark.live
@pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY") or not os.getenv("LIVE_SUGI_RECEIPT_IMAGE"),
    reason="set OPENROUTER_API_KEY and LIVE_SUGI_RECEIPT_IMAGE for the Sugi regression",
)
@override_settings(APP_AUTH="sugi-live-test")
def test_sugi_photo_extracts_the_printed_payment_and_coupon_scope(client):
    """Run the real upload and OCR with a local photo; image hosting stays mocked."""
    path = Path(os.environ["LIVE_SUGI_RECEIPT_IMAGE"])
    upload = SimpleUploadedFile(
        path.name, path.read_bytes(), _MEDIA_TYPES[path.suffix.lower()]
    )
    expected = json.loads((_FIXTURE_DIR / "sugi-receipt.json").read_text())

    async def local_translation(text, **kwargs):
        return SimpleNamespace(text=text)

    with (
        patch(
            "backend_api.api.cloudinary_upload_file",
            return_value="https://example.test/sugi",
        ),
        patch("backend_api.ocr.Translator") as translator,
    ):
        translator.return_value.translate = local_translation
        response = client.post(
            "/api/v1/receipts/receipt-items/",
            {"file": upload},
            HTTP_AUTHORIZATION="Bearer sugi-live-test",
        )
    assert response.status_code == 200
    data = response.json()
    lines = sorted(data["receipt_items"], key=lambda i: i["item_order"])
    assert [(i["cost"], i["discount"]) for i in lines] == [
        (i["cost"], i["discount"]) for i in expected["receipt_items"]
    ]
    assert data["receipt_discount"] == 233
    assert data["points_redeemed"] == 494
    [coupon] = data["split_rules"]["discounts"]
    assert coupon["amount"] == 233
    assert set(coupon["item_orders"]) == {lines[i]["item_order"] for i in (0, 1, 4, 5)}
    assert (
        sum(i["cost"] for i in lines)
        - data["receipt_discount"]
        - data["points_redeemed"]
        == data["total_amount"]
        == 5809
    )
