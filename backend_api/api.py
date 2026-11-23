import logging

from django.http import HttpRequest
from ninja import File, Router, UploadedFile
from pydantic_ai import BinaryContent

from backend_api.ocr import get_openrouter_receipt_agent
from backend_api.schemas import OCRReceiptPostOut
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
    result = await agent.run(
        [
            """
            here is the image receipt:
            """,
            BinaryContent(
                data=data, media_type=image_media_type(data, file.content_type)
            ),
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
