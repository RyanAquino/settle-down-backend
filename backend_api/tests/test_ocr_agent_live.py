"""Live OpenRouter test for the OCR agent.

This makes a REAL network call to OpenRouter (and costs credits). Its purpose is
to confirm the Gemini-via-OpenRouter path actually returns valid
strict-structured output — i.e. ``ToolOutput(ReceiptData, strict=True)`` plus
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
import os
from pathlib import Path

import pytest
from pydantic_ai import BinaryContent

from backend_api.dto.receipt_item import ReceiptData
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
    assert isinstance(data, ReceiptData)
    assert data.total_amount > 0
    assert data.receipt_items, "expected at least one extracted line item"
