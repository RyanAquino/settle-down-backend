"""SettleUpClient: authentication, caching and failure paths.

The happy-path money math lives in ``test_transaction.py`` and the payload
shape in ``test_create_transaction.py``; this module covers what surrounds them.
"""

from datetime import datetime
from typing import Any

import pytest
from django.conf import settings

from backend_api.schemas import TransactionPostIn, UserTransactionSchema
from backend_api.settleup_utils import SettleUpClient, SettleUpGroupNotFoundError
from backend_api.tests.conftest import FAKE_CREDS


def _payload(**overrides: Any) -> TransactionPostIn:
    fields: dict[str, Any] = {
        "purpose": "Lunch",
        "paying_member_id": "Member 1",
        "tax_percentage": 10,
        "total_amount": 200.0,
        "user_receipt_items": [
            UserTransactionSchema(member_id="Member 1", cost=100),
            UserTransactionSchema(member_id="Member 2", cost=100),
        ],
        "group_id": "group-1",
    }
    return TransactionPostIn(**(fields | overrides))


def _cache_hit_for(key_suffix, value):
    return lambda key: value if key.endswith(key_suffix) else None


class TestAuthentication:
    def test_signs_in_and_caches_the_token_on_a_miss(self, mock_settleup):
        client = SettleUpClient()

        sign_in = mock_settleup.firebase_auth.sign_in_with_email_and_password
        sign_in.assert_called_once_with(
            settings.SETTLE_UP_API_KEY,
            settings.SETTLE_UP_USER,
            settings.SETTLE_UP_PASSWORD,
            timeout=settings.UPSTREAM_HTTP_TIMEOUT,
        )
        mock_settleup.cache.set.assert_called_once_with(
            f"{settings.SETTLE_UP_USER}_token", timeout=3500, value=FAKE_CREDS
        )
        assert client.user_id == "test-local-id"
        assert client.auth_params == {"auth": "test-id-token"}

    def test_reuses_a_cached_token_without_signing_in(self, mock_settleup):
        cached = {"localId": "cached-id", "idToken": "cached-token"}
        mock_settleup.cache.get.side_effect = _cache_hit_for("_token", cached)

        client = SettleUpClient()

        mock_settleup.firebase_auth.sign_in_with_email_and_password.assert_not_called()
        assert client.auth_params == {"auth": "cached-token"}


class TestReadCaching:
    """Cached Settle Up reads are served without touching the network."""

    def test_groups(self, settle_up_client, mock_settleup):
        mock_settleup.cache.get.side_effect = _cache_hit_for("groups_v2", ["cached"])

        assert settle_up_client.get_groups() == ["cached"]
        mock_settleup.requests.get.assert_not_called()

    def test_group(self, settle_up_client, mock_settleup):
        group = {"name": "Cached", "convertedToCurrency": "EUR"}
        mock_settleup.cache.get.side_effect = _cache_hit_for("_settle_up_group", group)

        assert settle_up_client.get_group("group-1") == group
        mock_settleup.requests.get.assert_not_called()

    def test_members(self, settle_up_client, mock_settleup):
        members = [{"id": "m", "name": "Cached"}]
        mock_settleup.cache.get.side_effect = _cache_hit_for(
            "_settle_up_users", members
        )

        assert settle_up_client.get_group_members_by_group("group-1") == members
        mock_settleup.requests.get.assert_not_called()


class TestMissingGroup:
    """Firebase answers a nonexistent group with null; that must fail loudly."""

    @pytest.fixture(autouse=True)
    def _group_is_null(self, mock_settleup):
        routed = mock_settleup.requests.get.side_effect

        def null_groups(url, *args, **kwargs):
            response = routed(url, *args, **kwargs)
            if "/userGroups/" not in url and "/groups/" in url:
                response.json.return_value = None
            return response

        mock_settleup.requests.get.side_effect = null_groups

    def test_get_group_returns_none_and_does_not_cache(
        self, settle_up_client, mock_settleup
    ):
        assert settle_up_client.get_group("gone") is None
        cached_keys = [call.args[0] for call in mock_settleup.cache.set.call_args_list]
        assert "gone_settle_up_group" not in cached_keys

    def test_listing_groups_raises(self, settle_up_client, mock_settleup):
        mock_settleup.requests.get.return_value.json.return_value = {"gone": {}}

        with pytest.raises(SettleUpGroupNotFoundError, match="'gone'"):
            settle_up_client.get_groups()

    def test_creating_a_transaction_raises_before_posting(
        self, settle_up_client, mock_settleup
    ):
        with pytest.raises(SettleUpGroupNotFoundError, match="'group-1'"):
            settle_up_client.create_transaction(_payload())

        mock_settleup.requests.post.assert_not_called()


class TestCreateTransaction:
    def test_receipt_date_becomes_the_transaction_time(
        self, settle_up_client, mock_settleup
    ):
        # Naive on purpose: clients send receipt dates without an offset.
        receipt_date = datetime(2026, 5, 30, 12, 34, 56)  # noqa: DTZ001
        settle_up_client.create_transaction(_payload(receipt_date=receipt_date))

        body = mock_settleup.requests.post.call_args.kwargs["json"]
        assert body["dateTime"] == 1780144496000

    def test_members_owing_nothing_are_left_out_of_for_whom(
        self, settle_up_client, mock_settleup
    ):
        settle_up_client.create_transaction(
            _payload(
                total_amount=100.0,
                user_receipt_items=[
                    UserTransactionSchema(member_id="Member 1", cost=100),
                    UserTransactionSchema(member_id="Member 2", cost=0),
                ],
            )
        )

        body = mock_settleup.requests.post.call_args.kwargs["json"]
        assert body["items"][0]["forWhom"] == [{"memberId": "Member 1", "weight": "1"}]

    def test_posts_to_the_group_with_auth_and_a_timeout(
        self, settle_up_client, mock_settleup
    ):
        mock_settleup.requests.post.return_value.json.return_value = {"name": "tx-1"}

        result = settle_up_client.create_transaction(_payload())

        assert result == {"name": "tx-1"}
        call = mock_settleup.requests.post.call_args
        assert call.args == (
            f"{settings.SETTLE_UP_BASE_URL}/transactions/group-1.json",
        )
        assert call.kwargs["params"] == {"auth": "test-id-token"}
        assert call.kwargs["timeout"] == settings.UPSTREAM_HTTP_TIMEOUT
