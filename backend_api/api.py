import logging

from django.http import HttpRequest
from django.utils import timezone
from ninja import File, Router, UploadedFile
from ninja.errors import HttpError
from pydantic_ai import BinaryContent

from backend_api.dto.receipt_scan import ReceiptScan
from backend_api.ocr import ScanAttempts, get_openrouter_receipt_agent
from backend_api.receipt_allocation import (
    Allocation,
    PrintedReceipt,
    allocate,
    allocate_scan,
    corrected_receipt,
    from_minor_units,
)
from backend_api.schemas import (
    OCRReceiptItemOut,
    OCRReceiptPostOut,
    ReceiptAdjustmentOut,
    ReceiptAllocationItemOut,
    ReceiptAllocationOut,
    ReceiptAllocationShareOut,
    ReceiptCorrectionIn,
    ReceiptTaxLineOut,
    ReconciliationOut,
)
from backend_api.services import catbox_upload_file, cloudinary_upload_file

logger = logging.getLogger(__name__)

router = Router()

# Brands a phone writes in a HEIC photo's "ftyp" box (bytes 8-12).
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def image_media_type(data: bytes) -> str:
    """The media type of a receipt photo, read from its first bytes.

    Uploads are phone photos: JPEG from either camera, PNG screenshots, WebP
    from some Android galleries, or HEIC from iPhone and Android HEIF photos.
    The bytes decide, because the type a phone declares is often generic.
    Anything else is sent as JPEG, as every upload was before.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "image/heic"
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
                BinaryContent(data=data, media_type=image_media_type(data)),
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


@router.post("/allocations/", response={200: ReceiptAllocationOut})
def post_receipt_allocation(
    request: HttpRequest, payload: ReceiptCorrectionIn
) -> ReceiptAllocationOut:
    """Recalculate a scanned receipt with the items a person chose per adjustment.

    No model call and no image: the same allocation as the scan, with each
    adjustment applied to the items given. A choice that cannot be applied
    (an unknown item, or a discount larger than its items) is a 422.
    """
    try:
        receipt = corrected_receipt(
            decimal_places=payload.decimal_places,
            items=[
                (item.item_order, item.list_price) for item in payload.receipt_items
            ],
            adjustments=[
                (
                    adjustment.printed_label,
                    adjustment.kind,
                    adjustment.amount,
                    adjustment.percentage,
                    adjustment.eligible_subtotal,
                )
                for adjustment in payload.adjustments
            ],
            tax_lines=[
                (line.rate, line.taxable_amount, line.tax_amount)
                for line in payload.tax_lines
            ],
            amount_due=payload.total_amount,
        )
        allocation = allocate(
            receipt,
            scopes=[
                tuple(adjustment.item_orders) for adjustment in payload.adjustments
            ],
        )
    except ValueError as error:
        raise HttpError(422, str(error)) from error

    return ReceiptAllocationOut(
        receipt_items=[
            ReceiptAllocationItemOut(
                item_order=item.item_order,
                list_price=from_minor_units(item.price, receipt.decimal_places),
                cost=cost,
                discount=discount,
            )
            for item, (cost, discount) in zip(
                receipt.items, _costs(receipt, allocation), strict=True
            )
        ],
        adjustments=_adjustments(receipt, allocation),
        reconciliation=_reconciliation(receipt, allocation),
        tax_percentage=float(allocation.tax_rate or 0),
        total_amount=from_minor_units(allocation.total, receipt.decimal_places),
    )


def _costs(
    receipt: PrintedReceipt, allocation: Allocation
) -> list[tuple[float, float]]:
    """Each item's (cost, discount): what it cost and what discounts took off it.

    The discount is the item's share of every discount and points line, so it
    stays exact when tax or a surcharge was spread into the cost.
    """
    places = receipt.decimal_places
    reconciled = allocation.status == "reconciled"
    saved = dict.fromkeys(allocation.costs, 0)
    if reconciled:
        for entry in allocation.adjustments:
            if entry.adjustment.kind != "surcharge":
                for order, share in entry.allocations.items():
                    saved[order] += share
    return [
        (
            from_minor_units(allocation.costs[item.item_order], places),
            from_minor_units(saved[item.item_order], places),
        )
        for item in receipt.items
    ]


def _adjustments(
    receipt: PrintedReceipt, allocation: Allocation
) -> list[ReceiptAdjustmentOut]:
    places = receipt.decimal_places
    reconciled = allocation.status == "reconciled"
    return [
        ReceiptAdjustmentOut(
            printed_label=entry.adjustment.label,
            kind=entry.adjustment.kind,
            amount=from_minor_units(entry.adjustment.amount, places),
            percentage=(
                None
                if entry.adjustment.percentage is None
                else float(entry.adjustment.percentage)
            ),
            eligible_subtotal=(
                None
                if entry.adjustment.eligible_subtotal is None
                else from_minor_units(entry.adjustment.eligible_subtotal, places)
            ),
            item_orders=list(entry.item_orders),
            status=entry.status,
            # Shares only mean something once the whole receipt reconciles.
            allocations=(
                [
                    ReceiptAllocationShareOut(
                        item_order=order, amount=from_minor_units(share, places)
                    )
                    for order, share in entry.allocations.items()
                ]
                if reconciled
                else []
            ),
        )
        for entry in allocation.adjustments
    ]


def _reconciliation(
    receipt: PrintedReceipt, allocation: Allocation
) -> ReconciliationOut:
    return ReconciliationOut(
        status=allocation.status,
        difference=from_minor_units(allocation.difference, receipt.decimal_places),
    )


def _receipt_response(scan: ReceiptScan, image_url: str) -> OCRReceiptPostOut:
    """Map a transcription and its allocation onto the public scan response.

    ``cost`` is what each line cost after every adjustment, so the items add
    up to ``total_amount`` (or do with ``tax_percentage`` added on top). A scan
    that does not reconcile allocates nothing: every cost is 0 and the review
    step reads ``list_price``.
    """
    receipt, allocation = allocate_scan(scan)
    places = receipt.decimal_places
    return OCRReceiptPostOut(
        receipt_items=[
            OCRReceiptItemOut(
                english_name=printed.english_name,
                japanese_name=printed.japanese_name,
                item_order=item.item_order,
                cost=cost,
                quantity=printed.quantity,
                discount=discount,
                list_price=from_minor_units(item.price, places),
            )
            for item, printed, (cost, discount) in zip(
                receipt.items, scan.items, _costs(receipt, allocation), strict=True
            )
        ],
        en_shop_name=scan.en_shop_name,
        jp_shop_name=scan.jp_shop_name,
        tax_percentage=float(allocation.tax_rate or 0),
        total_amount=from_minor_units(allocation.total, places),
        receipt_date=scan.receipt_date or timezone.now(),
        receipt_image_url=image_url,
        decimal_places=places,
        adjustments=_adjustments(receipt, allocation),
        tax_lines=[
            ReceiptTaxLineOut(
                rate=None if line.rate is None else float(line.rate),
                taxable_amount=(
                    None
                    if line.taxable_amount is None
                    else from_minor_units(line.taxable_amount, places)
                ),
                tax_amount=(
                    None
                    if line.tax_amount is None
                    else from_minor_units(line.tax_amount, places)
                ),
            )
            for line in receipt.tax_lines
        ],
        reconciliation=_reconciliation(receipt, allocation),
    )
