"""Group-document fetching and the currency it carries.

The Settle Up group document (``groups/{group_id}.json``) is the source of
``convertedToCurrency`` — the group's currency (https://api.settleup.io/entities/).
"""

from django.conf import settings

from backend_api.dto.settleup import SettleUpGroup


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
        # Firebase RTDB returns a truthy error body (not an HTTP error status)
        # on auth/permission failures. Caching it would pin the failure for
        # the full ~24h group-cache timeout.
        mock_settleup.group_json.clear()
        mock_settleup.group_json["error"] = "Permission denied"

        group = settle_up_client.get_group("group-1")

        assert group == {"error": "Permission denied"}
        group_cache_calls = [
            call
            for call in mock_settleup.cache.set.call_args_list
            if call.args and call.args[0] == "group-1_settle_up_group"
        ]
        assert group_cache_calls == []


class TestGetGroups:
    def test_returns_each_groups_currency(self, settle_up_client, mock_settleup):
        # Fallback (non-/groups/) response serves the userGroups listing.
        mock_settleup.requests.get.return_value.json.return_value = {
            "group-1": {"member": "user-1"}
        }
        mock_settleup.group_json["convertedToCurrency"] = "TWD"

        groups = settle_up_client.get_groups()

        assert groups == [SettleUpGroup(name="Group A", id="group-1", currency="TWD")]

    def test_missing_currency_yields_none(self, settle_up_client, mock_settleup):
        # Fallback (non-/groups/) response serves the userGroups listing.
        mock_settleup.requests.get.return_value.json.return_value = {
            "group-1": {"member": "user-1"}
        }
        del mock_settleup.group_json["convertedToCurrency"]

        groups = settle_up_client.get_groups()

        assert groups == [SettleUpGroup(name="Group A", id="group-1", currency=None)]
