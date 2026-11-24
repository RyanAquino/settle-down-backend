import secrets

from django.conf import settings
from django.http import HttpRequest
from ninja import NinjaAPI, Swagger
from ninja.security import HttpBearer

from receipts.api import router as receipts_router
from settle_up.api import router as settle_up_router


class GlobalAuth(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> str | None:
        """Accept the request only if its bearer token equals ``settings.APP_AUTH``.

        The comparison is constant-time so response timing does not leak how much
        of a guessed token was right, and an unset ``APP_AUTH`` rejects every
        token rather than matching some degenerate value.

        Returns the token on success (ninja stores it on ``request.auth``), or
        None, which ninja turns into a 401.
        """
        expected = settings.APP_AUTH
        if expected and secrets.compare_digest(token.encode(), expected.encode()):
            return token
        return None


api = NinjaAPI(auth=GlobalAuth(), docs=Swagger(settings={"persistAuthorization": True}))

api.add_router("/v1/receipts/", receipts_router)
api.add_router("/v1/settle-up/", settle_up_router)
