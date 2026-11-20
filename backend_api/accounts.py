"""Per-device sessions. Only an opaque random bearer leaves the backend."""

import hashlib
import secrets
import time

import requests
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import HttpBearer
from pydantic import SecretStr

from backend_api.scans import allowance

router = Router()
IDENTITY = "https://identitytoolkit.googleapis.com/v1/accounts:"
REFRESH = "https://securetoken.googleapis.com/v1/token"
SESSION_TTL = 90 * 86400


def firebase(url, **kwargs):
    try:
        response = requests.post(
            url, params={"key": settings.SETTLE_UP_API_KEY}, timeout=10, **kwargs
        )
    except requests.RequestException:
        raise HttpError(503, "Settle Up sign-in is temporarily unavailable.") from None
    if response.status_code == 400:
        code = response.json().get("error", {}).get("message", "")
        if code.startswith("TOO_MANY_ATTEMPTS"):
            raise HttpError(429, "Too many sign-in attempts. Try again later.")
        if code in {
            "INVALID_LOGIN_CREDENTIALS",
            "INVALID_PASSWORD",
            "EMAIL_NOT_FOUND",
            "INVALID_EMAIL",
            "USER_DISABLED",
            "TOKEN_EXPIRED",
            "INVALID_REFRESH_TOKEN",
            "USER_NOT_FOUND",
            "INVALID_GRANT",
            "INVALID_ID_TOKEN",
        }:
            raise HttpError(401, "Sign in with your Settle Up email and password.")
    if not response.ok:
        raise HttpError(503, "Settle Up sign-in is temporarily unavailable.")
    return response.json()


def credentials(tokens):
    token = tokens["idToken"]
    user = firebase(IDENTITY + "lookup", json={"idToken": token})["users"][0]
    return dict(
        uid=user["localId"],
        email=user["email"],
        verified=user.get("emailVerified") is True,
        token=token,
        refresh=tokens["refreshToken"],
        expires=time.time() + int(tokens["expiresIn"]) - 60,
    )


def session_key(token):
    return "session:" + hashlib.sha256(token.encode()).hexdigest()


class SessionAuth(HttpBearer):
    def authenticate(self, request, token):
        key = session_key(token)
        account = cache.get(key)
        if not account:
            return None
        if account["expires"] <= time.time():
            # Re-read under the lock so concurrent callers share one refresh and
            # a sign-out racing a refresh cannot resurrect the session.
            with cache.lock(key + ":lock", timeout=40, blocking_timeout=35):
                account = cache.get(key)
                if not account:
                    return None
                if account["expires"] <= time.time():
                    try:
                        data = firebase(
                            REFRESH,
                            data={
                                "grant_type": "refresh_token",
                                "refresh_token": account["refresh"],
                            },
                        )
                        account = credentials(
                            dict(
                                idToken=data["id_token"],
                                refreshToken=data["refresh_token"],
                                expiresIn=data["expires_in"],
                            )
                        )
                    except HttpError as exc:
                        if exc.status_code == 401:
                            cache.delete(key)
                        raise
                    cache.set(key, account, SESSION_TTL)
        request.session_cache_key = key
        return account


class AsyncSessionAuth(SessionAuth):
    async def authenticate(self, request, token):
        return await sync_to_async(super().authenticate)(request, token)


class Login(Schema):
    email: str
    password: SecretStr


@router.post("/sign-in/", auth=None)
def sign_in(request, payload: Login):
    tokens = firebase(
        IDENTITY + "signInWithPassword",
        json={
            "email": payload.email,
            "password": payload.password.get_secret_value(),
            "returnSecureToken": True,
        },
    )
    account = credentials(tokens)
    token = secrets.token_urlsafe(32)
    cache.set(session_key(token), account, SESSION_TTL)
    return {"token": token, **allowance(account)}


@router.get("/me/")
def me(request):
    return allowance(request.auth)


@router.post("/sign-out/", response={204: None})
def sign_out(request):
    key = request.session_cache_key
    with cache.lock(key + ":lock", timeout=40, blocking_timeout=35):
        cache.delete(key)
    return 204, None
