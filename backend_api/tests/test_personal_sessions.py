"""Run the HTTP flow against a dedicated Redis with only external services stubbed."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import requests
from django.core.files.uploadedfile import SimpleUploadedFile

from backend_api.accounts import session_key
from backend_api.dto.receipt_item import ReceiptData

TEST_PASSWORD = "fixture-only"
ITEM = dict(english_name="Tea", japanese_name="茶", quantity=1, item_order=1, cost=100)
RECEIPT = ReceiptData(
    receipt_items=[ITEM],
    en_shop_name="Test Shop",
    jp_shop_name="テスト",
    total_amount=100,
)


def external_request(self, method, url, **options):
    """Local provider fixtures, including per-user database permissions."""
    body = options.get("json", {})
    status = 200
    if url.endswith("signInWithPassword"):
        if body["password"] != TEST_PASSWORD:
            status, data = 400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}}
        else:
            email = body["email"]
            data = {"idToken": email, "refreshToken": email, "expiresIn": "3600"}
    elif url.endswith("lookup"):
        email = body["idToken"]
        data = {
            "users": [
                {
                    "email": email,
                    "localId": email.split("@")[0],
                    "emailVerified": not email.startswith("unverified"),
                }
            ]
        }
    elif url.endswith("/v1/token"):
        email = options["data"]["refresh_token"]
        data = {"id_token": email, "refresh_token": email, "expires_in": "3600"}
    else:
        user = options["params"]["auth"].split("@")[0]
        group = "group-" + user
        if url.endswith(f"/userGroups/{user}.json"):
            data = {group: True}
        elif url.endswith(f"/groups/{group}.json"):
            data = {"name": group, "convertedToCurrency": "JPY"}
        elif url.endswith(f"/members/{group}.json"):
            data = {"member": {"name": "Member"}}
        elif method.lower() == "post" and url.endswith(f"/transactions/{group}.json"):
            data = {"name": "expense"}
        else:
            status, data = 403, {"error": "Permission denied"}
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(data).encode()
    return response


def provider_patches():
    return (
        patch("requests.sessions.Session.request", external_request),
        patch(
            "backend_api.api.get_openrouter_receipt_agent",
            return_value=SimpleNamespace(
                run=AsyncMock(return_value=SimpleNamespace(output=RECEIPT))
            ),
        ),
        patch(
            "backend_api.api.cloudinary_upload_file",
            return_value="https://example.invalid/receipt.png",
        ),
    )


@pytest.mark.skipif(
    not os.getenv("TEST_REDIS_URL"), reason="requires a dedicated test Redis"
)
def test_account_and_scan_flow(client, settings):
    from django.core.cache import cache

    settings.CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": os.environ["TEST_REDIS_URL"],
        }
    }
    settings.DAILY_SCAN_LIMIT = 1
    user = uuid4().hex
    email = user + "@example.test"
    second = "another-" + email
    settings.SCAN_LIMIT_EXEMPT_EMAILS = {second, "unverified-" + email}
    auth, ocr, host = provider_patches()
    with auth, ocr as agent, host:

        def login(address, password=TEST_PASSWORD):
            return client.post(
                "/api/v1/auth/sign-in/",
                {"email": address, "password": password},
                content_type="application/json",
            )

        def upload(session, scan_id, image=b"fixture-image"):
            return client.post(
                "/api/v1/receipts/receipt-items/",
                {"file": SimpleUploadedFile("sample.jpg", image)},
                headers={
                    "Authorization": "Bearer " + session,
                    "Idempotency-Key": str(scan_id),
                },
            )

        def headers(token):
            return {"Authorization": "Bearer " + token}

        assert login(email, "incorrect").status_code == 401
        assert client.get("/api/v1/auth/me/").status_code == 401
        session = login(email).json()["token"]
        device2 = login(email).json()["token"]
        other = login(second).json()["token"]
        assert login("unverified-" + email).json()["remaining"] == 1
        account = cache.get(session_key(session))
        account["expires"] = 0
        cache.set(session_key(session), account)
        assert (
            client.get("/api/v1/auth/me/", headers=headers(session)).json()["remaining"]
            == 1
        )
        group = "group-" + user
        assert (
            client.get("/api/v1/settle-up/groups/", headers=headers(session)).json()[
                "items"
            ][0]["id"]
            == group
        )
        assert (
            client.get(
                "/api/v1/settle-up/users/", {"group_id": group}, headers=headers(other)
            ).status_code
            == 403
        )
        scan_id = uuid4()
        agent.return_value.run.side_effect = RuntimeError("upstream unavailable")
        with pytest.raises(RuntimeError):
            upload(session, scan_id)
        agent.return_value.run.side_effect = None
        assert upload(session, scan_id).status_code == 200
        assert upload(device2, scan_id).status_code == 200
        assert upload(session, scan_id, b"different").status_code == 400
        assert upload(device2, uuid4()).status_code == 429
        assert agent.return_value.run.await_count == 2
        assert (
            client.get("/api/v1/auth/me/", headers=headers(device2)).json()["remaining"]
            == 0
        )
        assert upload(other, uuid4()).status_code == 200
        assert upload(other, uuid4()).status_code == 200
        assert (
            client.get("/api/v1/auth/me/", headers=headers(other)).json()["remaining"]
            is None
        )
        payload = {
            "purpose": "Tea",
            "paying_member_id": "member",
            "tax_percentage": 0,
            "total_amount": 100,
            "group_id": group,
            "user_receipt_items": [{"member_id": "member", "cost": 100}],
        }
        assert (
            client.post(
                "/api/v1/settle-up/transactions/",
                payload,
                content_type="application/json",
                headers=headers(session),
            ).status_code
            == 204
        )
        assert (
            client.post("/api/v1/auth/sign-out/", headers=headers(session)).status_code
            == 204
        )
        assert (
            client.get("/api/v1/auth/me/", headers=headers(session)).status_code == 401
        )
        assert (
            client.get("/api/v1/auth/me/", headers=headers(device2)).status_code == 200
        )


@pytest.mark.skipif(
    not os.getenv("TEST_REDIS_URL"), reason="requires a dedicated test Redis"
)
def test_concurrent_scan_and_midnight_reset(settings):
    from datetime import datetime, timedelta
    from ninja.errors import HttpError
    from backend_api.scans import JAPAN, Scan, allowance

    settings.CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": os.environ["TEST_REDIS_URL"],
        }
    }
    settings.DAILY_SCAN_LIMIT = 1
    account = {"uid": uuid4().hex, "email": "limited@example.test", "verified": False}
    first = Scan(account, uuid4(), "image")
    second = Scan(account, uuid4(), "different")
    # Use a future midnight so Redis (which uses its real clock) retains both keys.
    before = datetime.combine(
        datetime.now(JAPAN).date() + timedelta(days=1), datetime.max.time(), JAPAN
    )
    with patch("backend_api.scans.datetime", wraps=datetime) as clock:
        clock.now.return_value = before
        first.begin()
        try:
            with pytest.raises(HttpError) as busy:
                second.begin()
            assert busy.value.status_code == 409
            first.finish({"receipt": "test"})
        finally:
            first.close()
        assert allowance(account)["remaining"] == 0
        clock.now.return_value = before + timedelta(seconds=1)
        assert allowance(account)["remaining"] == 1
        second.begin()
        second.close()
