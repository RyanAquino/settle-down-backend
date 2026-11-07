"""Firebase Authentication over its REST API.

Settle Up stores its data in a Firebase Realtime Database, and every REST call
is authorized with a Firebase ID token obtained by signing the service account
in with email and password. That sign-in is the only thing this app needs from
Firebase Auth, so it calls the documented endpoint directly rather than through
``pyrebase4`` (which pins ``urllib3<2`` and drags in ``gcloud``/``oauth2client``):
https://firebase.google.com/docs/reference/rest/auth#section-sign-in-email-password
"""

from typing import TypedDict

import requests

SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"


class FirebaseCredentials(TypedDict):
    """The fields of a sign-in response this app reads (Firebase returns more)."""

    localId: str
    idToken: str


def sign_in_with_email_and_password(
    api_key: str, email: str, password: str, *, timeout: float
) -> FirebaseCredentials:
    """Sign in a Firebase email/password account and return its credentials.

    Raises ``requests.HTTPError`` carrying Firebase's error body (for example
    ``INVALID_LOGIN_CREDENTIALS``) when the sign-in is rejected.
    """
    response = requests.post(
        SIGN_IN_URL,
        params={"key": api_key},
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=timeout,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise requests.HTTPError(f"{exc}: {response.text}", response=response) from exc

    credentials: FirebaseCredentials = response.json()
    return credentials
