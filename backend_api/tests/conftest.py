from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend_api.settleup_utils import SettleUpClient

# Fake Firebase credentials returned by the mocked sign-in. The shape mirrors
# what pyrebase returns so SettleUpClient.__init__ can read `localId`/`idToken`.
FAKE_CREDS = {"localId": "test-local-id", "idToken": "test-id-token"}

# Stubbed SettleUp REST response for `members/{group_id}.json`. The transaction
# assertions in this suite depend on the group having exactly these two members.
GROUP_MEMBERS = {
    "Member 1": {"name": "Member 1"},
    "Member 2": {"name": "Member 2"},
}


@pytest.fixture
def mock_settleup():
    """Patch every external boundary ``SettleUpClient`` touches.

    - Firebase auth (``pyrebase``): the real ``sign_in_with_email_and_password``
      login is replaced with fake credentials, so constructing a
      ``SettleUpClient`` performs **no real login**.
    - The SettleUp REST API (``requests``) used to fetch group members.
    - The cache, forced to always miss so the mocked login/REST paths run and
      Redis is never contacted.

    Yields the mocks so individual tests can tailor responses (for example a
    different member set) before exercising the client.
    """
    with patch("backend_api.settleup_utils.pyrebase") as mock_pyrebase, patch(
        "backend_api.settleup_utils.requests"
    ) as mock_requests, patch("backend_api.settleup_utils.cache") as mock_cache:
        # Force a cache miss so the (mocked) login and REST paths actually run.
        mock_cache.get.return_value = None

        # No real Firebase login — return fake credentials.
        auth = mock_pyrebase.initialize_app.return_value.auth.return_value
        auth.sign_in_with_email_and_password.return_value = FAKE_CREDS

        # Stub the SettleUp REST members endpoint.
        mock_requests.get.return_value.json.return_value = GROUP_MEMBERS

        yield SimpleNamespace(
            pyrebase=mock_pyrebase, requests=mock_requests, cache=mock_cache
        )


@pytest.fixture
def settle_up_client(mock_settleup):
    """A ready-to-use ``SettleUpClient`` with all boundaries mocked.

    Reusable across the whole transaction suite — request a ``settle_up_client``
    argument in any test to get a client that performs no real login, network,
    or Redis traffic.
    """
    return SettleUpClient()
