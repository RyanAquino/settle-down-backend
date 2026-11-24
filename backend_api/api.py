from django.http import HttpRequest
from ninja import File, Router, UploadedFile
from ninja.errors import HttpError

from backend_api.ocr import scan_receipt
from backend_api.receipt_responses import (
    allocation_response,
    corrected_allocation,
    scan_response,
)
from backend_api.schemas import (
    OCRReceiptPostOut,
    ReceiptAllocationOut,
    ReceiptCorrectionIn,
)
from backend_api.services import upload_receipt_image

router = Router()


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(
    request: HttpRequest, file: File[UploadedFile]
) -> OCRReceiptPostOut:
    scan = await scan_receipt(file.read())
    return scan_response(scan, upload_receipt_image(file))


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
        receipt, allocation = corrected_allocation(payload)
    except ValueError as error:
        raise HttpError(422, str(error)) from error
    return allocation_response(receipt, allocation)
