import cloudinary
import cloudinary.uploader
import requests
from django.conf import settings
from ninja import UploadedFile


def catbox_upload_file(file_obj: UploadedFile) -> str:
    """Upload to catbox.moe and return the hosted file's URL."""
    file_obj.seek(0)
    response = requests.post(
        "https://catbox.moe/user/api.php",
        data={"reqtype": "fileupload"},
        files={"fileToUpload": file_obj},
        timeout=settings.UPSTREAM_HTTP_TIMEOUT,
    )
    return response.text.strip()


def cloudinary_upload_file(file_obj: UploadedFile) -> str:
    """Upload to Cloudinary and return the hosted file's HTTPS URL."""
    file_obj.seek(0)
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )

    upload_result = cloudinary.uploader.upload(
        file_obj, timeout=settings.UPSTREAM_HTTP_TIMEOUT
    )
    url: str = upload_result["secure_url"]
    return url
