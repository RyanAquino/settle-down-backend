import logging

from django.http import HttpRequest
from django.utils import timezone
from ninja import File, Router, UploadedFile
from pydantic_ai import BinaryContent

from backend_api.dto.receipt_scan import ReceiptScan
from backend_api.ocr import ScanAttempts, get_openrouter_receipt_agent
from backend_api.receipt_allocation import allocate_scan, from_minor_units
from backend_api.schemas import OCRReceiptItemOut, OCRReceiptPostOut
from backend_api.services import catbox_upload_file, cloudinary_upload_file

logger = logging.getLogger(__name__)

router = Router()

# ISO base media file brands that mark a HEIC/HEIF still image (bytes 8-12).
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"}
_HEIF_BRANDS = {b"mif1", b"msf1", b"heif"}


def image_media_type(data: bytes, declared: str | None) -> str:
    """The media type of an uploaded receipt photo, read from its leading bytes.

    The bytes are authoritative: the declared multipart type depends on the
    client and is often missing or generic. An unrecognised file falls back to
    the declared type when it names an image, else to JPEG, so the model still
    receives every upload as an image, as it did before types were detected.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        if data[8:12] in _HEIC_BRANDS:
            return "image/heic"
        if data[8:12] in _HEIF_BRANDS:
            return "image/heif"
    if declared and declared.startswith("image/"):
        return declared
    return "image/jpeg"


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(
    request: HttpRequest, file: File[UploadedFile]
) -> OCRReceiptPostOut:
    agent = get_openrouter_receipt_agent()

    data = file.read()
    attempts = ScanAttempts()
    try:
        result = await agent.run(
            [
                """
                here is the image receipt:
                """,
                BinaryContent(
                    data=data, media_type=image_media_type(data, file.content_type)
                ),
            ],
            deps=attempts,
        )
        scan = result.output
    except Exception:
        # A later read (or the model call itself) failed after an earlier read
        # was already usable as money: answer with that rather than an error.
        best = attempts.best()
        if best is None:
            raise
        logger.warning(
            "Receipt read failed after a usable attempt; using the best attempt",
            exc_info=True,
        )
        scan = best

    try:
        url = cloudinary_upload_file(file)
    except Exception:
        # Any Cloudinary failure (config, network, API error) falls back to
        # catbox; keep the traceback so the root cause is not lost.
        logger.warning(
            "Cloudinary upload failed; falling back to catbox", exc_info=True
        )
        url = catbox_upload_file(file)

    return _receipt_response(scan, url)


def _receipt_response(scan: ReceiptScan, image_url: str) -> OCRReceiptPostOut:
    """Map a transcription and its allocation onto the public scan response.

    ``cost`` is what each line cost after every adjustment, so the items add
    up to ``total_amount`` (or do with ``tax_percentage`` added on top). A scan
    that does not reconcile keeps its printed prices instead.
    """
    receipt, allocation = allocate_scan(scan)
    places = receipt.decimal_places
    reconciled = allocation.status == "reconciled"
    items = []
    for item, printed in zip(receipt.items, scan.items, strict=True):
        cost = allocation.costs[item.item_order]
        saved = item.price - cost if reconciled else 0
        items.append(
            OCRReceiptItemOut(
                english_name=printed.english_name,
                japanese_name=printed.japanese_name,
                item_order=item.item_order,
                cost=from_minor_units(cost, places),
                quantity=printed.quantity,
                discount=from_minor_units(max(saved, 0), places),
            )
        )
    return OCRReceiptPostOut(
        receipt_items=items,
        en_shop_name=scan.en_shop_name,
        jp_shop_name=scan.jp_shop_name,
        tax_percentage=float(allocation.tax_rate or 0),
        total_amount=from_minor_units(allocation.total, places),
        receipt_date=scan.receipt_date or timezone.now(),
        receipt_image_url=image_url,
    )
