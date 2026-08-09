import logging

from ninja import Router, File, UploadedFile
from pydantic_ai import BinaryContent

from .ocr import get_openrouter_receipt_agent
from .schemas import OCRReceiptPostOut
from .services import catbox_upload_file, cloudinary_upload_file
from .utils import spread_item_quantities

logger = logging.getLogger(__name__)

router = Router()


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(request, file: File[UploadedFile]):
    agent = get_openrouter_receipt_agent()

    result = await agent.run(
        [
            """
            here is the image receipt:
            """,
            BinaryContent(data=file.read(), media_type="image/jpg"),
        ]
    )
    results = result.output
    # Spread "Shake x3" into three quantity-1 items so each unit can be
    # assigned to a different group member.
    results.receipt_items = spread_item_quantities(results.receipt_items)

    try:
        url = cloudinary_upload_file(file)
    except Exception as e:
        logger.warning("Cloudinary upload failed; falling back to catbox: %s", e)
        url = catbox_upload_file(file)

    # receipt_date is None for undated receipts; dropping it lets
    # OCRReceiptPostOut's default_factory fill in the current time.
    return OCRReceiptPostOut(
        **results.model_dump(exclude_none=True), receipt_image_url=url
    )
