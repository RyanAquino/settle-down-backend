from ninja import NinjaAPI
from backend_api.api import router as backend_api_router
from backend_api.settleup_api import router as settleup_api_router

api = NinjaAPI()

api.add_router("/v1/receipts/", backend_api_router)
api.add_router("/v1/settle-up/", settleup_api_router)
