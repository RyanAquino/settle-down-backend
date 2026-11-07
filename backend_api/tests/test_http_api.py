"""The HTTP API end to end: URL routing, middleware, bearer auth and every view.

Requests go through Django's test ``Client``, so they traverse the real URL
conf, middleware stack and ninja's auth exactly as a deployed request does.
Only the outbound boundaries are mocked: Settle Up (via ``mock_settleup``) and
the OCR agent and image hosts (patched per test).
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from pydantic_ai import BinaryContent

from backend_api.dto.receipt_item import ReceiptData, ReceiptItemData

TOKEN = "test-app-token"
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

GROUPS_URL = "/api/v1/settle-up/groups/"
USERS_URL = "/api/v1/settle-up/users/"
TRANSACTIONS_URL = "/api/v1/settle-up/transactions/"
RECEIPT_URL = "/api/v1/receipts/receipt-items/"


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

    def test_create_transaction_files_it_in_settle_up(self, client, mock_settleup):
        response = client.post(
            TRANSACTIONS_URL,
            {
                "purpose": "Lunch",
                "paying_member_id": "Member 1",
                "tax_percentage": 10,
                "total_amount": 200.0,
                "user_receipt_items": [
                    {"member_id": "Member 1", "cost": 100},
                    {"member_id": "Member 2", "cost": 100},
                ],
                "group_id": "group-1",
            },
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 204
        assert response.content == b""
        body = mock_settleup.requests.post.call_args.kwargs["json"]
        assert body["purpose"] == "Lunch"
        assert body["currencyCode"] == "JPY"
        assert body["whoPaid"] == [{"memberId": "Member 1", "weight": "200.0"}]

    def test_create_transaction_validates_the_payload(self, client, mock_settleup):
        response = client.post(
            TRANSACTIONS_URL,
            {"purpose": "Lunch"},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 422
        mock_settleup.requests.post.assert_not_called()


RECEIPT = ReceiptData(
    receipt_items=[
        ReceiptItemData(
            english_name="Onigiri",
            japanese_name="おにぎり",
            item_order=1,
            cost=300,
            quantity=2,
        )
    ],
    en_shop_name="Sample Mart",
    jp_shop_name="サンプルマート",
    tax_percentage=10,
    total_amount=330,
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
