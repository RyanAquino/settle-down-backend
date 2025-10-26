from django.conf import settings
from ninja import NinjaAPI, Swagger
from ninja.security import HttpBearer

from backend_api.api import router as backend_api_router
from backend_api.settleup_api import router as settleup_api_router


class GlobalAuth(HttpBearer):
    def authenticate(self, request, token):
        if token == settings.APP_AUTH:
            return token


api = NinjaAPI(auth=GlobalAuth(), docs=Swagger(settings={"persistAuthorization": True}))

api.add_router("/v1/receipts/", backend_api_router)
api.add_router("/v1/settle-up/", settleup_api_router)
