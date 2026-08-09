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


class TestGetGroups:
    def test_returns_each_groups_currency(self, settle_up_client, mock_settleup):
        # Fallback (non-/groups/) response serves the userGroups listing.
        mock_settleup.requests.get.return_value.json.return_value = {
            "group-1": {"member": "user-1"}
        }
        mock_settleup.group_json["convertedToCurrency"] = "TWD"

        groups = settle_up_client.get_groups()

        assert groups == [SettleUpGroup(name="Group A", id="group-1", currency="TWD")]
