from asgiref.sync import sync_to_async
from django.http import HttpRequest
from django.utils import timezone
from ninja import File, Router, UploadedFile
from ninja.errors import HttpError

from receipts.allocation import allocate_scan, from_minor_units
from receipts.image_hosting import upload_receipt_image
from receipts.ocr import read_receipt
from receipts.schemas import (
    OCRReceiptItemOut,
    OCRReceiptPostOut,
    ReceiptAllocationOut,
    ReceiptCorrectionIn,
    ReceiptTaxLineOut,
)
from receipts.services import (
    ReceiptCorrectionError,
    adjustments_out,
    item_costs,
    recalculate_receipt,
    reconciliation_out,
)

router = Router(tags=["receipts"])


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(
    request: HttpRequest, file: File[UploadedFile]
) -> OCRReceiptPostOut:
    """Read a receipt photo and say what each line cost.

    ``cost`` is what each line cost after every adjustment, so the items add
    up to ``total_amount`` (or do with ``tax_percentage`` added on top). A scan
    that does not reconcile allocates nothing: every cost is 0 and the review
    step reads ``list_price``.
    """
    scan = await read_receipt(file.read())
    image_url = await sync_to_async(upload_receipt_image)(file)
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
                receipt.items, scan.items, item_costs(receipt, allocation), strict=True
            )
        ],
        en_shop_name=scan.en_shop_name,
        jp_shop_name=scan.jp_shop_name,
        tax_percentage=float(allocation.tax_rate or 0),
        total_amount=from_minor_units(allocation.total, places),
        receipt_date=scan.receipt_date or timezone.now(),
        receipt_image_url=image_url,
        decimal_places=places,
        adjustments=adjustments_out(receipt, allocation),
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
        reconciliation=reconciliation_out(receipt, allocation),
    )


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
        return recalculate_receipt(payload)
    except ReceiptCorrectionError as error:
        raise HttpError(422, str(error)) from error
