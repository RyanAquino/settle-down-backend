import pytest

from backend_api.api import image_media_type

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
HEIF = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00"
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00"


class TestImageMediaType:
    """The model is told the type the photo's bytes show."""

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (JPEG, "image/jpeg"),
            (PNG, "image/png"),
            (WEBP, "image/webp"),
            (HEIC, "image/heic"),
            (HEIF, "image/heic"),
        ],
    )
    def test_each_phone_format_is_read_from_its_bytes(self, data, expected):
        assert image_media_type(data) == expected

    @pytest.mark.parametrize("data", [b"", b"unknown", AVIF])
    def test_anything_else_is_sent_as_jpeg(self, data):
        assert image_media_type(data) == "image/jpeg"
