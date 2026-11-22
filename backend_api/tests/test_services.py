"""Receipt-image hosting: Cloudinary (primary) and catbox.moe (fallback)."""

from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from backend_api.services import catbox_upload_file, cloudinary_upload_file


def _read_upload():
    """An upload whose read position is at the end, as after the OCR read."""
    upload = SimpleUploadedFile("receipt.jpg", b"jpeg-bytes", content_type="image/jpeg")
    upload.read()
    return upload


def test_catbox_upload_rewinds_posts_and_returns_the_url():
    upload = _read_upload()

    with patch("backend_api.services.requests.post") as post:
        post.return_value.text = "https://files.catbox.moe/abc.jpg\n"
        url = catbox_upload_file(upload)

    assert url == "https://files.catbox.moe/abc.jpg"
    (endpoint,) = post.call_args.args
    assert endpoint == "https://catbox.moe/user/api.php"
    assert post.call_args.kwargs["data"] == {"reqtype": "fileupload"}
    assert post.call_args.kwargs["timeout"] == settings.UPSTREAM_HTTP_TIMEOUT
    sent = post.call_args.kwargs["files"]["fileToUpload"]
    assert sent.read() == b"jpeg-bytes"  # rewound before upload


@override_settings(
    CLOUDINARY_CLOUD_NAME="cloud",
    CLOUDINARY_API_KEY="key",
    CLOUDINARY_API_SECRET="secret",
)
def test_cloudinary_upload_configures_uploads_and_returns_the_https_url():
    upload = _read_upload()

    with (
        patch("backend_api.services.cloudinary.config") as config,
        patch("backend_api.services.cloudinary.uploader.upload") as cloud_upload,
    ):
        cloud_upload.return_value = {
            "secure_url": "https://res.cloudinary.com/r.jpg",
            "url": "http://res.cloudinary.com/r.jpg",
        }
        url = cloudinary_upload_file(upload)

    assert url == "https://res.cloudinary.com/r.jpg"
    config.assert_called_once_with(
        cloud_name="cloud", api_key="key", api_secret="secret", secure=True
    )
    (sent,) = cloud_upload.call_args.args
    assert sent.read() == b"jpeg-bytes"  # rewound before upload
    assert cloud_upload.call_args.kwargs == {"timeout": settings.UPSTREAM_HTTP_TIMEOUT}
