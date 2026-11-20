from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend_api.settleup_utils import SettleUpClient

# Stubbed SettleUp REST response for `members/{group_id}.json`. The transaction
# assertions in this suite depend on the group having exactly these two members.
GROUP_MEMBERS = {
    "Member 1": {"name": "Member 1"},
    "Member 2": {"name": "Member 2"},
}

# Stubbed SettleUp REST response for `groups/{group_id}.json` — the group
# document that carries the group's currency. The default is JPY so the
# long-standing payload-pinning tests keep seeing the currency they pinned.
GROUP_INFO = {"name": "Group A", "convertedToCurrency": "JPY"}


@pytest.fixture
def mock_settleup():
    """Replace the Settle Up REST boundary."""
    with patch("backend_api.settleup_utils.requests") as mock_requests:
        # Stub the SettleUp REST members endpoint.
        mock_requests.get.return_value.json.return_value = GROUP_MEMBERS

        # Route group-document fetches to a dedicated response; every other URL
        # falls through to `return_value`, so tests that retarget the members
        # or userGroups endpoints via `return_value.json.return_value` keep
        # working. `/userGroups/` is excluded because it contains `/groups/`
        # only as a case-insensitive substring of a different endpoint.
        group_json = dict(GROUP_INFO)

        def _route_get(url, *args, **kwargs):
            if "/userGroups/" not in url and "/groups/" in url:
                resp = MagicMock()
                resp.json.return_value = group_json
                return resp
            return mock_requests.get.return_value

        mock_requests.get.side_effect = _route_get

        yield SimpleNamespace(
            requests=mock_requests,
            group_json=group_json,
        )


@pytest.fixture
def settle_up_client(mock_settleup):
    """A ready-to-use ``SettleUpClient`` with all boundaries mocked.

    Reusable across the whole transaction suite — request a ``settle_up_client``
    argument in any test to get a client that performs no real login, network,
    or Redis traffic.
    """
    return SettleUpClient("test-local-id", "test-id-token")
