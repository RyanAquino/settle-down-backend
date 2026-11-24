"""Every route sits behind the project's bearer token (``settledown.api.GlobalAuth``).

Requests go through Django's test ``Client``, so the real URL conf, middleware
and ninja's auth run. The receipt allocations route calls nothing outside the
process, so it stands in for every route without any mocks.
"""

from unittest.mock import patch

import pytest
from django.test import override_settings

TOKEN = "test-app-token"
URL = "/api/v1/receipts/allocations/"
BODY = {
    "decimal_places": 0,
    "receipt_items": [{"item_order": 1, "list_price": 100}],
    "adjustments": [],
    "total_amount": 100,
}


@pytest.fixture(autouse=True)
def _app_token():
    with override_settings(APP_AUTH=TOKEN):
        yield


def _post(client, **headers):
    return client.post(URL, BODY, content_type="application/json", **headers)


class TestBearerAuth:
    def test_missing_token_is_rejected(self, client):
        with patch("receipts.api.recalculate_receipt") as recalculate:
            response = _post(client)

        assert response.status_code == 401
        recalculate.assert_not_called()

    def test_wrong_token_is_rejected(self, client):
        response = _post(client, HTTP_AUTHORIZATION="Bearer not-the-token")

        assert response.status_code == 401

    def test_non_bearer_scheme_is_rejected(self, client):
        response = _post(client, HTTP_AUTHORIZATION=f"Basic {TOKEN}")

        assert response.status_code == 401

    @pytest.mark.parametrize("unset", [None, ""])
    def test_unset_app_auth_rejects_every_token(self, client, unset):
        with override_settings(APP_AUTH=unset):
            response = _post(client, HTTP_AUTHORIZATION="Bearer ")

        assert response.status_code == 401

    def test_correct_token_is_accepted(self, client):
        response = _post(client, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

        assert response.status_code == 200
