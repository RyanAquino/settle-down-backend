"""The Settle Up routes over HTTP: URL routing, middleware, auth and each view.

Requests go through Django's test ``Client``, so they traverse the real URL
conf, middleware stack and ninja's auth exactly as a deployed request does.
Only Settle Up itself is mocked, via ``mock_settleup``.
"""

import pytest
from django.test import override_settings

from settle_up.tests.conftest import firebase_answer

TOKEN = "test-app-token"
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

GROUPS_URL = "/api/v1/settle-up/groups/"
USERS_URL = "/api/v1/settle-up/users/"
TRANSACTIONS_URL = "/api/v1/settle-up/transactions/"

LUNCH = {
    "purpose": "Lunch",
    "paying_member_id": "Member 1",
    "user_receipt_items": [
        {"member_id": "Member 1", "cost": 1},
        {"member_id": "Member 2", "cost": 2},
    ],
    "group_id": "group-1",
}

# A group id goes into the Firebase REST path: these would point the
# service account's reads and writes at another node, add a query, or
# cut the path.
STEERING_IDS = (
    "../userGroups/test-local-id",
    "group-1/x",
    "group-1.json?print=pretty",
    "group-1#",
    "G%31",
    "group$1",
    "group[1]",
    "",
)


@pytest.fixture(autouse=True)
def _app_token():
    with override_settings(APP_AUTH=TOKEN):
        yield


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

    def test_a_group_id_may_be_a_push_id(self, client, mock_settleup):
        response = client.get(USERS_URL, {"group_id": "-Nx_9aZ"}, **AUTH)

        assert response.status_code == 200
        (url,) = mock_settleup.requests.get.call_args.args
        assert url.endswith("/members/-Nx_9aZ.json")

    @pytest.mark.parametrize("group_id", STEERING_IDS)
    def test_list_users_refuses_a_group_id_that_changes_the_path(
        self, client, mock_settleup, group_id
    ):
        response = client.get(USERS_URL, {"group_id": group_id}, **AUTH)

        assert response.status_code == 422
        mock_settleup.requests.get.assert_not_called()

    @pytest.mark.parametrize("group_id", STEERING_IDS)
    def test_create_transaction_refuses_a_group_id_that_changes_the_path(
        self, client, mock_settleup, group_id
    ):
        response = client.post(
            TRANSACTIONS_URL,
            {**LUNCH, "tax_percentage": 10, "total_amount": 3, "group_id": group_id},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][-1] == "group_id"
        mock_settleup.requests.post.assert_not_called()

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

    @pytest.mark.parametrize(
        ("changes", "field"),
        [
            ({"total_amount": "NaN"}, "total_amount"),
            ({"total_amount": "Infinity"}, "total_amount"),
            (
                {"user_receipt_items": [{"member_id": "Member 1", "cost": "NaN"}]},
                "cost",
            ),
            (
                {
                    "user_receipt_items": [
                        {"member_id": "Member 1", "cost": "-Infinity"}
                    ]
                },
                "cost",
            ),
            ({"user_receipt_items": [{"member_id": "Member 1", "cost": -1}]}, "cost"),
            ({"split_receipt_items": ["Infinity"]}, 0),
            ({"split_receipt_items": [-1]}, 0),
        ],
    )
    def test_create_transaction_rejects_amounts_that_are_not_money(
        self, client, mock_settleup, changes, field
    ):
        response = client.post(
            TRANSACTIONS_URL,
            {**LUNCH, "tax_percentage": 10, "total_amount": 3, **changes},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][-1] == field
        mock_settleup.requests.post.assert_not_called()

    @pytest.mark.parametrize(
        "items",
        [
            {"user_receipt_items": []},
            {"user_receipt_items": [{"member_id": "Member 1", "cost": 0}]},
            # Half a cent each for the two members: nothing at cent precision.
            {"user_receipt_items": [], "split_receipt_items": [0.01]},
        ],
    )
    def test_create_transaction_with_nothing_to_split_is_refused(
        self, client, mock_settleup, items
    ):
        response = client.post(
            TRANSACTIONS_URL,
            {**LUNCH, "tax_percentage": 10, "total_amount": 3, **items},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 422
        assert "Nothing to split" in response.json()["detail"]
        mock_settleup.requests.post.assert_not_called()

    @pytest.mark.parametrize(
        ("answer", "status"),
        [
            # Settle Up refused the write, so nothing was filed: the app keeps
            # the draft and says Settle Up rejected it.
            (firebase_answer(401, {"error": "Auth token is expired"}), 424),
            (firebase_answer(403, {"error": "Permission denied"}), 424),
            (firebase_answer(400, {"error": "Invalid data"}), 424),
            # Settle Up failed: it may have filed it, so the app must not resend
            # it blindly (a 5xx is "unconfirmed" to the app).
            (firebase_answer(503, {"error": "Service unavailable"}), 502),
        ],
    )
    def test_create_transaction_reports_a_write_settle_up_did_not_accept(
        self, client, mock_settleup, answer, status
    ):
        mock_settleup.requests.post.return_value = answer

        response = client.post(
            TRANSACTIONS_URL,
            {**LUNCH, "tax_percentage": 10, "total_amount": 3},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == status
        assert answer.json()["error"] in response.json()["detail"]
        mock_settleup.requests.post.assert_called_once()

    def test_create_transaction_validates_the_payload(self, client, mock_settleup):
        response = client.post(
            TRANSACTIONS_URL,
            {"purpose": "Lunch"},
            content_type="application/json",
            **AUTH,
        )

        assert response.status_code == 422
        mock_settleup.requests.post.assert_not_called()
