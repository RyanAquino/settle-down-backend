from django.http import HttpRequest
from ninja import Router, Status
from ninja.pagination import paginate

from settle_up.dto import SettleUpGroup, SettleUpMember
from settle_up.schemas import (
    SettleUpGroupSchema,
    SettleUpUserSchema,
    TransactionPostIn,
)
from settle_up.services import SettleUpClient

router = Router(tags=["settle-up"])


@router.get("/groups/", response={200: list[SettleUpGroupSchema]})
@paginate
def get_settle_up_groups(request: HttpRequest) -> list[SettleUpGroup]:
    settle_up_client = SettleUpClient()
    return settle_up_client.get_groups()


@router.get("/users/", response={200: list[SettleUpUserSchema]})
@paginate
def get_settle_up_users(request: HttpRequest, group_id: str) -> list[SettleUpMember]:
    settle_up_client = SettleUpClient()
    return settle_up_client.get_group_members_by_group(group_id)


@router.post("/transactions/", response={204: None})
def post_settle_up_create_transaction(
    request: HttpRequest, payload: TransactionPostIn
) -> Status[None]:
    settle_up_client = SettleUpClient()
    settle_up_client.create_transaction(payload)

    return Status(204, None)
