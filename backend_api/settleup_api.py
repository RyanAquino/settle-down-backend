import requests
from django.contrib.auth.models import User
from ninja import Router
from ninja.pagination import paginate

from backend_api.models import SettleUpGroup
from backend_api.serializer import SettleUpGroupSchema, TransactionPostIn, SettleUpUserSchema
from backend_api.settleup_utils import SettleUpClient
from django.conf import settings

router = Router()


@router.get("/groups/", response={200: list[SettleUpGroupSchema]})
@paginate
def get_settle_up_groups(request):
    settle_up_client = SettleUpClient()
    groups = settle_up_client.get_groups()

    return groups


@router.get("/users/", response={200: list[SettleUpUserSchema]})
@paginate
def get_settle_up_users(request, group_id: str):
    settle_up_client = SettleUpClient()
    return settle_up_client.get_group_members_by_group(group_id)



@router.post("/sync-users/", response={204: None})
def post_settle_up_groups(request):
    settle_up_client = SettleUpClient()
    groups = settle_up_client.get_groups()
    settle_up_client.get_or_create_group_members(groups)

    return 204, None


@router.post("/transactions/", response={204: None})
def post_settle_up_create_transaction(request, payload: TransactionPostIn):
    settle_up_client = SettleUpClient()
    settle_up_client.create_transaction(payload)

    return 204, None

