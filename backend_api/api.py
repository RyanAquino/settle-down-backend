import logging

from ninja import Router, File, UploadedFile
from pydantic_ai import BinaryContent

from .ocr import get_receipt_agent
from .schemas import OCRReceiptPostOut
from .services import catbox_upload_file, cloudinary_upload_file

logger = logging.getLogger(__name__)

router = Router()


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(request, file: File[UploadedFile]):
    agent = get_receipt_agent()

    result = await agent.run(
        [
            """
            here is the image receipt:
            """,
            BinaryContent(data=file.read(), media_type="image/jpg"),
        ]
    )
    results = result.output

    try:
        url = cloudinary_upload_file(file)
    except Exception as e:
        logger.warning("Cloudinary upload failed; falling back to catbox: %s", e)
        url = catbox_upload_file(file)

    return OCRReceiptPostOut(**results.model_dump(), receipt_image_url=url)
