import hashlib
import logging
from uuid import UUID

from asgiref.sync import sync_to_async
from ninja import File, Header, Router, UploadedFile
from pydantic_ai import BinaryContent

from .accounts import AsyncSessionAuth
from .ocr import get_openrouter_receipt_agent
from .scans import Scan, reconciles
from .schemas import OCRReceiptPostOut
from .services import catbox_upload_file, cloudinary_upload_file

logger = logging.getLogger(__name__)

router = Router(auth=AsyncSessionAuth())


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(
    request,
    file: File[UploadedFile],
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
):
    image = file.read()
    scan = Scan(request.auth, idempotency_key, hashlib.sha256(image).hexdigest())
    cached = await sync_to_async(scan.begin)()
    try:
        if cached is not None:
            return cached
        result = await extract_receipt(file, image)
        if reconciles(result):
            await sync_to_async(scan.finish)(result.model_dump(mode="json"))
        return result
    finally:
        await sync_to_async(scan.close)()


async def extract_receipt(file, image):
    agent = get_openrouter_receipt_agent()

    result = await agent.run(
        [
            """
            here is the image receipt:
            """,
            BinaryContent(data=image, media_type="image/jpg"),
        ]
    )
    results = result.output

    try:
        url = await sync_to_async(cloudinary_upload_file)(file)
    except Exception as e:
        logger.warning("Cloudinary upload failed; falling back to catbox: %s", e)
        url = await sync_to_async(catbox_upload_file)(file)

    return OCRReceiptPostOut(**results.model_dump(), receipt_image_url=url)
