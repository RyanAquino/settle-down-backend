"""The HTTP API end to end: URL routing, middleware, bearer auth and every view.

Requests go through Django's test ``Client``, so they traverse the real URL
conf, middleware stack and ninja's auth exactly as a deployed request does.
Only the outbound boundaries are mocked: Settle Up (via ``mock_settleup``) and
the OCR agent and image hosts (patched per test).
"""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from pydantic_ai import BinaryContent, UnexpectedModelBehavior

from backend_api.dto.receipt_scan import ReceiptScan
from backend_api.receipt_allocation import allocate_scan
from backend_api.tests import client_gate, scans

TOKEN = "test-app-token"
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

GROUPS_URL = "/api/v1/settle-up/groups/"
USERS_URL = "/api/v1/settle-up/users/"
TRANSACTIONS_URL = "/api/v1/settle-up/transactions/"
RECEIPT_URL = "/api/v1/receipts/receipt-items/"

LUNCH = {
    "purpose": "Lunch",
    "paying_member_id": "Member 1",
    "user_receipt_items": [
        {"member_id": "Member 1", "cost": 1},
        {"member_id": "Member 2", "cost": 2},
    ],
    "group_id": "group-1",
}


@pytest.fixture(autouse=True)
def _app_token():
    with override_settings(APP_AUTH=TOKEN):
        yield


@pytest.fixture
def client():
    return Client()


class TestBearerAuth:
    def test_missing_token_is_rejected(self, client, mock_settleup):
        response = client.get(GROUPS_URL)

        assert response.status_code == 401
        mock_settleup.firebase_auth.sign_in_with_email_and_password.assert_not_called()

    def test_wrong_token_is_rejected(self, client, mock_settleup):
        response = client.get(GROUPS_URL, HTTP_AUTHORIZATION="Bearer not-the-token")

        assert response.status_code == 401

    def test_non_bearer_scheme_is_rejected(self, client, mock_settleup):
        response = client.get(GROUPS_URL, HTTP_AUTHORIZATION=f"Basic {TOKEN}")

        assert response.status_code == 401

    @pytest.mark.parametrize("unset", [None, ""])
    def test_unset_app_auth_rejects_every_token(self, client, mock_settleup, unset):
        with override_settings(APP_AUTH=unset):
            response = client.get(GROUPS_URL, HTTP_AUTHORIZATION="Bearer ")

        assert response.status_code == 401

    def test_correct_token_is_accepted(self, client, mock_settleup):
        mock_settleup.requests.get.return_value.json.return_value = {}

        response = client.get(GROUPS_URL, **AUTH)

        assert response.status_code == 200


class TestSettleUpEndpoints:
    def test_list_groups_is_paginated(self, client, mock_settleup):
        mock_settleup.requests.get.return_value.json.return_value = {
            "group-1": {"member": "user-1"}
        }

        response = client.get(GROUPS_URL, **AUTH)

        assert response.status_code == 200
        assert response.json() == {
            "items": [{"name": "Group A", "id": "group-1", "currency": "JPY"}],
            "count": 1,
        }

    def test_list_users_of_a_group(self, client, mock_settleup):
        response = client.get(USERS_URL, {"group_id": "group-1"}, **AUTH)

        assert response.status_code == 200
        assert response.json() == {
            "items": [
                {"name": "Member 1", "id": "Member 1"},
                {"name": "Member 2", "id": "Member 2"},
            ],
            "count": 2,
        }

    def test_list_users_requires_group_id(self, client, mock_settleup):
        response = client.get(USERS_URL, **AUTH)

        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("tax_percentage", "total", "weights"),
        [(10, 3.30, ["1", "2"]), (8.25, 3.25, ["108", "217"])],
    )
    def test_create_transaction_files_it_in_settle_up(
        self, client, mock_settleup, tax_percentage, total, weights
    ):
        mock_settleup.group_json["convertedToCurrency"] = "USD"
        response = client.post(
            TRANSACTIONS_URL,
            {**LUNCH, "tax_percentage": tax_percentage, "total_amount": total},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 204
        assert response.content == b""
        body = mock_settleup.requests.post.call_args.kwargs["json"]
        assert body["purpose"] == "Lunch"
        assert body["currencyCode"] == "USD"
        assert body["whoPaid"] == [{"memberId": "Member 1", "weight": str(total)}]
        assert body["items"] == [
            {
                "amount": str(total),
                "forWhom": [
                    {"memberId": "Member 1", "weight": weights[0]},
                    {"memberId": "Member 2", "weight": weights[1]},
                ],
            }
        ]

    @pytest.mark.parametrize("rate", ["not a rate", "NaN", "Infinity", "-Infinity"])
    def test_create_transaction_rejects_invalid_tax(self, client, mock_settleup, rate):
        response = client.post(
            TRANSACTIONS_URL,
            {**LUNCH, "tax_percentage": rate, "total_amount": 3},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][-1] == "tax_percentage"
        mock_settleup.requests.post.assert_not_called()

    def test_create_transaction_validates_the_payload(self, client, mock_settleup):
        response = client.post(
            TRANSACTIONS_URL,
            {"purpose": "Lunch"},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 422
        mock_settleup.requests.post.assert_not_called()


# A plain receipt as the OCR model transcribes it: one line, 10% tax added on
# top. Its response is the same as before discounts were allocated.
RECEIPT = ReceiptScan.model_validate(
    {
        "jp_shop_name": "サンプルマート",
        "en_shop_name": "Sample Mart",
        # The model reports the receipt's JST wall time, without an offset.
        "receipt_date": "2026-05-30T12:34:56",
        "decimal_places": 0,
        "items": [
            {
                "item_order": 1,
                "japanese_name": "おにぎり",
                "english_name": "Onigiri",
                "quantity": 2,
                "printed_price": 300,
            }
        ],
        "adjustments": [],
        "tax_lines": [{"rate": 10, "taxable_amount": None, "tax_amount": 30}],
        "amount_due": 330,
    }
)


@pytest.fixture
def ocr_agent():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=SimpleNamespace(output=RECEIPT))
    with patch("backend_api.api.get_openrouter_receipt_agent", return_value=agent):
        yield agent


def _upload():
    return SimpleUploadedFile("receipt.jpg", b"jpeg-bytes", content_type="image/jpeg")


class TestReceiptEndpoint:
    def test_response_body_is_the_scan_contract(self, client, ocr_agent):
        """The exact body the installed app parses: every field, nothing else."""
        with patch(
            "backend_api.api.cloudinary_upload_file",
            return_value="https://res.cloudinary.com/r.jpg",
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        assert response.json() == {
            "receipt_items": [
                {
                    "english_name": "Onigiri",
                    "japanese_name": "おにぎり",
                    "item_order": 1,
                    "cost": 300.0,
                    "quantity": 2,
                    "discount": 0.0,
                }
            ],
            "en_shop_name": "Sample Mart",
            "jp_shop_name": "サンプルマート",
            "tax_percentage": 10.0,
            "total_amount": 330.0,
            "receipt_date": "2026-05-30T12:34:56",
            "receipt_image_url": "https://res.cloudinary.com/r.jpg",
        }

    def test_extracts_receipt_and_hosts_image_on_cloudinary(self, client, ocr_agent):
        with (
            patch(
                "backend_api.api.cloudinary_upload_file",
                return_value="https://res.cloudinary.com/r.jpg",
            ),
            patch("backend_api.api.catbox_upload_file") as catbox,
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        data = response.json()
        assert data["receipt_image_url"] == "https://res.cloudinary.com/r.jpg"
        assert data["en_shop_name"] == "Sample Mart"
        assert data["total_amount"] == 330
        assert [item["english_name"] for item in data["receipt_items"]] == ["Onigiri"]
        catbox.assert_not_called()

        # The image bytes reach the model as binary content.
        prompt = ocr_agent.run.await_args.args[0]
        images = [part for part in prompt if isinstance(part, BinaryContent)]
        assert [image.data for image in images] == [b"jpeg-bytes"]

    def test_tells_the_model_the_real_image_type(self, client, ocr_agent):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        upload = SimpleUploadedFile(
            "receipt.png", png, content_type="application/octet-stream"
        )
        with patch(
            "backend_api.api.cloudinary_upload_file",
            return_value="https://res.cloudinary.com/r.png",
        ):
            response = client.post(RECEIPT_URL, {"file": upload}, **AUTH)

        assert response.status_code == 200
        prompt = ocr_agent.run.await_args.args[0]
        (image,) = [part for part in prompt if isinstance(part, BinaryContent)]
        assert image.media_type == "image/png"
        assert image.data == png

    def test_falls_back_to_catbox_when_cloudinary_fails(
        self, client, ocr_agent, caplog
    ):
        with (
            patch(
                "backend_api.api.cloudinary_upload_file",
                side_effect=RuntimeError("cloudinary down"),
            ),
            patch(
                "backend_api.api.catbox_upload_file",
                return_value="https://files.catbox.moe/r.jpg",
            ),
            caplog.at_level(logging.WARNING, logger="backend_api.api"),
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        assert response.json()["receipt_image_url"] == "https://files.catbox.moe/r.jpg"
        # The Cloudinary failure is logged with its traceback, not swallowed.
        (record,) = caplog.records
        assert "falling back to catbox" in record.getMessage()
        assert record.exc_info[1].args == ("cloudinary down",)

    def test_requires_a_file(self, client, ocr_agent):
        response = client.post(RECEIPT_URL, {}, **AUTH)

        assert response.status_code == 422
        ocr_agent.run.assert_not_awaited()


def _scan_response(client, ocr_agent, scan):
    ocr_agent.run.return_value = SimpleNamespace(output=scan)
    with patch(
        "backend_api.api.cloudinary_upload_file",
        return_value="https://res.cloudinary.com/r.jpg",
    ):
        response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)
    assert response.status_code == 200
    return response.json()


def _accepted_by_installed_app(body):
    items = body["receipt_items"]
    return client_gate.accepts(
        costs=[item["cost"] for item in items],
        tax_percentage=body["tax_percentage"],
        total=body["total_amount"],
        item_orders=[item["item_order"] for item in items],
    )


class TestDiscountedReceipts:
    """Item costs carry every discount, so installed apps need no release."""

    def test_each_line_costs_its_share_after_every_discount(self, client, ocr_agent):
        body = _scan_response(client, ocr_agent, scans.scan(scans.vector("sugi")))

        assert [
            (item["item_order"], item["cost"], item["discount"])
            for item in body["receipt_items"]
        ] == [
            (1, 837.0, 150.0),
            (2, 744.0, 133.0),
            (3, 1809.0, 501.0),
            (4, 1533.0, 425.0),
            (5, 650.0, 117.0),
            (6, 236.0, 42.0),
        ]
        assert body["total_amount"] == 5809.0
        assert body["tax_percentage"] == 10.0
        assert body["jp_shop_name"] == "テスト薬局"
        assert body["receipt_date"] == "2026-11-23T15:16:00"
        assert _accepted_by_installed_app(body)

    def test_a_receipt_a_person_must_review_keeps_its_printed_prices(
        self, client, ocr_agent
    ):
        ambiguous = scans.vector("sugi_both_item_discounts_read_under_one_item")

        body = _scan_response(client, ocr_agent, scans.scan(ambiguous))

        assert [item["cost"] for item in body["receipt_items"]] == [
            987.0,
            877.0,
            2310.0,
            1958.0,
            767.0,
            278.0,
        ]
        assert {item["discount"] for item in body["receipt_items"]} == {0.0}
        assert body["total_amount"] == 5809.0
        # Fail closed: the installed app rejects it rather than file a guess.
        assert not _accepted_by_installed_app(body)

    def test_cents_are_returned_as_printed(self, client, ocr_agent):
        cents = scans.vector("coupon_before_tax_in_cents")

        body = _scan_response(client, ocr_agent, scans.scan(cents))

        assert [item["cost"] for item in body["receipt_items"]] == [11.72, 6.75, 4.51]
        assert body["tax_percentage"] == 8.25
        assert body["total_amount"] == 24.88
        assert _accepted_by_installed_app(body)

    def test_a_receipt_without_a_date_is_dated_now(self, client, ocr_agent):
        now = datetime(2026, 11, 23, 6, 16, tzinfo=UTC)
        undated = scans.scan(scans.vector("sugi"), receipt_date=None)

        with patch("backend_api.api.timezone.now", return_value=now):
            body = _scan_response(client, ocr_agent, undated)

        assert body["receipt_date"] == "2026-11-23T06:16:00Z"


class TestFailedReads:
    def test_a_read_that_fails_after_a_usable_attempt_answers_with_it(
        self, client, ocr_agent, caplog
    ):
        usable = scans.scan(scans.vector("sugi_points_line_dropped"))

        async def fail_after_one_attempt(prompt, deps):
            deps.record(usable, allocate_scan(usable)[1])
            raise UnexpectedModelBehavior("Exceeded maximum output retries (2)")

        ocr_agent.run.side_effect = fail_after_one_attempt
        with (
            patch(
                "backend_api.api.cloudinary_upload_file",
                return_value="https://res.cloudinary.com/r.jpg",
            ),
            caplog.at_level(logging.WARNING, logger="backend_api.api"),
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["total_amount"] == 5809.0
        assert not _accepted_by_installed_app(body)
        (record,) = caplog.records
        assert "using the best attempt" in record.getMessage()
        assert isinstance(record.exc_info[1], UnexpectedModelBehavior)

    def test_a_read_with_no_usable_attempt_is_an_error(self, client, ocr_agent):
        ocr_agent.run.side_effect = UnexpectedModelBehavior("Exceeded retries")

        with pytest.raises(UnexpectedModelBehavior):
            client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)
