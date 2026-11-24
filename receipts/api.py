from django.http import HttpRequest
from ninja import File, Router, UploadedFile
from ninja.errors import HttpError

from receipts.image_hosting import upload_receipt_image
from receipts.ocr import read_receipt
from receipts.schemas import (
    OCRReceiptPostOut,
    ReceiptAllocationOut,
    ReceiptCorrectionIn,
)
from receipts.services import (
    ReceiptCorrectionError,
    recalculate_receipt,
    scan_response,
)

router = Router(tags=["receipts"])


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(
    request: HttpRequest, file: File[UploadedFile]
) -> OCRReceiptPostOut:
    scan = await read_receipt(file.read())
    image_url = upload_receipt_image(file)
    return scan_response(scan, image_url)


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
