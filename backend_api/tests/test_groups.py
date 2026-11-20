"""Group-document fetching and the currency it carries.

The Settle Up group document (``groups/{group_id}.json``) is the source of
``convertedToCurrency`` — the currency the group is configured in
(https://api.settleup.io/entities/). Every transaction is filed in it.
"""

from django.conf import settings


class TestGetGroup:
    def test_returns_group_document_for_signed_in_account(
        self, settle_up_client, mock_settleup
    ):
        group = settle_up_client.get_group("group-1")

        assert group == {"name": "Group A", "convertedToCurrency": "JPY"}
        mock_settleup.requests.get.assert_called_with(
            f"{settings.SETTLE_UP_BASE_URL}/groups/group-1.json",
            params=settle_up_client.auth_params,
            timeout=15,
        )


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
