"""Live OpenRouter test for the OCR agent.

This makes a REAL network call to OpenRouter (and costs credits). Its purpose is
to confirm the Gemini-via-OpenRouter path actually returns valid
strict-structured output, i.e. ``ToolOutput(ReceiptScan, strict=True)`` plus
image input round-trips through ``settings.OPENROUTER_MODEL``, which a mocked
unit test cannot verify.

It is skipped unless BOTH are present, so the normal suite (and CI, which has no
key) stays green:

  - ``OPENROUTER_API_KEY``: a real key (``settings`` reads it from env/.env)
  - a receipt image at ``$LIVE_RECEIPT_IMAGE`` (falls back to
    ``receipts/tests/fixtures/receipt.<jpg|jpeg|png>``)

Run it explicitly with::

    OPENROUTER_API_KEY=... LIVE_RECEIPT_IMAGE=/path/to/receipt.jpg uv run pytest -m live
"""

import asyncio
import os
from pathlib import Path

import pytest
from pydantic_ai import BinaryContent

from receipts.allocation import allocate_scan
from receipts.ocr import ScanAttempts, get_openrouter_receipt_agent, image_media_type
from receipts.transcription import ReceiptScan

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_SUFFIXES = (".jpg", ".jpeg", ".png")


def _receipt_image() -> Path | None:
    """Locate a receipt image: $LIVE_RECEIPT_IMAGE, else a default fixture."""
    env_path = os.getenv("LIVE_RECEIPT_IMAGE")
    if env_path:
        candidate = Path(env_path)
        return candidate if candidate.is_file() else None
    for suffix in _SUFFIXES:
        candidate = _FIXTURE_DIR / f"receipt{suffix}"
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.live
@pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"),
    reason="live test: set OPENROUTER_API_KEY to run",
)
def test_openrouter_live_transcribes_a_receipt_that_can_be_allocated():
    image_path = _receipt_image()
    if image_path is None:
        pytest.skip(
            "live test: no receipt image (set LIVE_RECEIPT_IMAGE or add "
            "receipts/tests/fixtures/receipt.jpg)"
        )

    get_openrouter_receipt_agent.cache_clear()
    agent = get_openrouter_receipt_agent()
    data = image_path.read_bytes()

    async def _run():
        return await agent.run(
            [
                "here is the image receipt:",
                BinaryContent(data=data, media_type=image_media_type(data)),
            ],
            deps=ScanAttempts(),
        )

    result = asyncio.run(_run())
    scan = result.output

    # If agent.run() returned at all, the ToolOutput schema was honored by the
    # model and the transcription could be read as money. One stochastic call
    # cannot pin exact amounts, so assert what every outcome must satisfy.
    assert isinstance(scan, ReceiptScan)
    assert scan.items, "expected at least one transcribed item"
    _, allocation = allocate_scan(scan)
    assert allocation.status in {"reconciled", "needs_review", "mismatch"}
    assert all(cost >= 0 for cost in allocation.costs.values())
    if allocation.status == "reconciled" and not allocation.tax_added:
        assert sum(allocation.costs.values()) == allocation.total
