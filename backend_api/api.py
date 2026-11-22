import logging

from django.http import HttpRequest
from ninja import File, Router, UploadedFile
from pydantic_ai import BinaryContent

from backend_api.ocr import get_openrouter_receipt_agent
from backend_api.schemas import OCRReceiptPostOut
from backend_api.services import catbox_upload_file, cloudinary_upload_file

logger = logging.getLogger(__name__)

router = Router()


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(
    request: HttpRequest, file: File[UploadedFile]
) -> OCRReceiptPostOut:
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

    try:
        url = cloudinary_upload_file(file)
    except Exception:
        # Any Cloudinary failure (config, network, API error) falls back to
        # catbox; keep the traceback so the root cause is not lost.
        logger.warning(
            "Cloudinary upload failed; falling back to catbox", exc_info=True
        )
        url = catbox_upload_file(file)

    return OCRReceiptPostOut(**results.model_dump(), receipt_image_url=url)
