import cloudinary
import cloudinary.uploader
import requests
from django.conf import settings
from ninja import UploadedFile


def catbox_upload_file(file_obj: UploadedFile):
    """
    Upload to catbox.moe to retrieve file URL.

    Args:
        file_obj: UploadedFile

    Returns:
    """
    file_obj.seek(0)
    response = requests.post(
        "https://catbox.moe/user/api.php",
        data={"reqtype": "fileupload"},
        files={"fileToUpload": file_obj},
    )
    return response.text.strip()


def cloudinary_upload_file(file_obj: UploadedFile):
    file_obj.seek(0)
    cloudinary.config(
        cloud_name="dpbqobzo9",
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )

    upload_result = cloudinary.uploader.upload(file_obj)
    return upload_result["secure_url"]
