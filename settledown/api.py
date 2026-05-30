from django.conf import settings
from ninja import NinjaAPI, Swagger
from ninja.security import HttpBearer

from backend_api.api import router as backend_api_router
from backend_api.settleup_api import router as settleup_api_router


class GlobalAuth(HttpBearer):
    def authenticate(self, request, token) -> str | None:
        """
        Authenticates a given request token against the application settings.

        This function checks if the provided token matches the application
        authentication token specified in the settings. If the token is valid,
        it will return the token; otherwise, it does nothing (implicit handling
        assumed in this partial implementation).

        Args:
            request: The incoming request object to be authenticated.
            token: The authentication token provided for verification.

        Returns:
            str: The valid authentication token if it matches the application settings.
        """
        if token == settings.APP_AUTH:
            return token
        return None


api = NinjaAPI(auth=GlobalAuth(), docs=Swagger(settings={"persistAuthorization": True}))

api.add_router("/v1/receipts/", backend_api_router)
api.add_router("/v1/settle-up/", settleup_api_router)
