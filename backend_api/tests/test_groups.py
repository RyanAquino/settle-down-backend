"""Group-document fetching and the currency it carries.

The Settle Up group document (``groups/{group_id}.json``) is the source of
``convertedToCurrency`` — the currency the group is configured in
(https://api.settleup.io/entities/). Every transaction is filed in it.
"""

from django.conf import settings


class TestGetGroup:
    def test_returns_group_document_and_caches_it(
        self, settle_up_client, mock_settleup
    ):
        group = settle_up_client.get_group("group-1")

        assert group == {"name": "Group A", "convertedToCurrency": "JPY"}
        mock_settleup.requests.get.assert_called_with(
            f"{settings.SETTLE_UP_BASE_URL}/groups/group-1.json",
            params=settle_up_client.auth_params,
        )
        mock_settleup.cache.set.assert_called_with(
            "group-1_settle_up_group", timeout=86500, value=group
        )

    def test_does_not_cache_firebase_error_body(self, settle_up_client, mock_settleup):
        # Firebase RTDB signals auth/permission failures with a truthy error
        # body rather than an HTTP error status. Caching that would pin the
        # failure for the full ~24h group-cache timeout.
        mock_settleup.group_json.clear()
        mock_settleup.group_json["error"] = "Permission denied"

        group = settle_up_client.get_group("group-1")

        assert group == {"error": "Permission denied"}
        group_cache_writes = [
            call
            for call in mock_settleup.cache.set.call_args_list
            if call.args and call.args[0] == "group-1_settle_up_group"
        ]
        assert group_cache_writes == []


class TestGetGroups:
    def test_returns_each_groups_currency(self, settle_up_client, mock_settleup):
        # The fallback (non-/groups/) response serves the userGroups listing.
        mock_settleup.requests.get.return_value.json.return_value = {
            "group-1": {"member": "user-1"}
        }
        mock_settleup.group_json["convertedToCurrency"] = "TWD"

        groups = settle_up_client.get_groups()

        # Assert on attributes, not on equality with a constructed SettleUpGroup:
        # pydantic ignores an unknown `currency=` kwarg, so that comparison would
        # pass even with no currency field at all.
        assert [(g.name, g.id, g.currency) for g in groups] == [
            ("Group A", "group-1", "TWD")
        ]

    def test_missing_currency_yields_none(self, settle_up_client, mock_settleup):
        mock_settleup.requests.get.return_value.json.return_value = {
            "group-1": {"member": "user-1"}
        }
        del mock_settleup.group_json["convertedToCurrency"]

        groups = settle_up_client.get_groups()

        # Tolerant here by design: one malformed group must not break the whole
        # listing. Strictness lives at transaction time.
        assert [(g.name, g.id, g.currency) for g in groups] == [
            ("Group A", "group-1", None)
        ]
