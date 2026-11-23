import pytest

from backend_api.api import image_media_type

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
HEIF = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00"
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00"


class TestImageMediaType:
    """The model is told the real image type, whatever the client declared."""

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (JPEG, "image/jpeg"),
            (PNG, "image/png"),
            (WEBP, "image/webp"),
            (HEIC, "image/heic"),
            (HEIF, "image/heif"),
        ],
    )
    def test_the_file_signature_wins_over_the_declared_type(self, data, expected):
        assert image_media_type(data, "application/octet-stream") == expected
        assert image_media_type(data, "image/jpeg") == expected

    def test_an_unknown_image_keeps_its_declared_image_type(self):
        assert image_media_type(AVIF, "image/avif") == "image/avif"
        assert image_media_type(b"unknown", "image/gif") == "image/gif"

    @pytest.mark.parametrize("declared", [None, "", "application/octet-stream"])
    def test_anything_else_is_still_sent_as_an_image(self, declared):
        assert image_media_type(b"unknown", declared) == "image/jpeg"
        assert image_media_type(AVIF, declared) == "image/jpeg"
