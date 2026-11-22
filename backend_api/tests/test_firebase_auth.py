"""Firebase email/password sign-in over the Firebase Auth REST API."""

import json
from unittest.mock import patch

import pytest
import requests

from backend_api.firebase_auth import SIGN_IN_URL, sign_in_with_email_and_password


def _response(status: int, body: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.url = SIGN_IN_URL
    return response


def test_signs_in_and_returns_the_credentials():
    body = {"localId": "uid-1", "idToken": "id-token", "refreshToken": "r"}

    with patch(
        "backend_api.firebase_auth.requests.post", return_value=_response(200, body)
    ) as post:
        credentials = sign_in_with_email_and_password(
            "api-key", "user@example.com", "hunter2", timeout=5
        )

    assert credentials == body
    post.assert_called_once_with(
        SIGN_IN_URL,
        params={"key": "api-key"},
        json={
            "email": "user@example.com",
            "password": "hunter2",
            "returnSecureToken": True,
        },
        timeout=5,
    )


def test_rejected_sign_in_raises_with_firebase_error_detail():
    body = {"error": {"code": 400, "message": "INVALID_LOGIN_CREDENTIALS"}}

    with (
        patch(
            "backend_api.firebase_auth.requests.post",
            return_value=_response(400, body),
        ),
        pytest.raises(requests.HTTPError, match="INVALID_LOGIN_CREDENTIALS") as exc,
    ):
        sign_in_with_email_and_password("api-key", "u@example.com", "x", timeout=5)

    assert exc.value.response.status_code == 400
