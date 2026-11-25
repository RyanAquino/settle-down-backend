"""The project serves the API and nothing else.

Every API route sits behind the bearer token. A route outside it answers
anyone, so none is mounted: in particular no Django admin, which has no
models to manage and no database tables to run on.
"""

import pytest


@pytest.mark.parametrize("path", ["/admin/", "/admin/login/"])
def test_there_is_no_admin_site(client, path):
    # With the admin mounted, a made-up session cookie made it read a session
    # table that does not exist: a 500 anyone could cause, without the token.
    client.cookies["sessionid"] = "x" * 32

    response = client.get(path)

    assert response.status_code == 404
