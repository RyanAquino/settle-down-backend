from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError
from ninja.pagination import paginate

from settle_up.dto import SettleUpGroup, SettleUpMember
from settle_up.schemas import (
    GroupId,
    SettleUpGroupSchema,
    SettleUpUserSchema,
    TransactionPostIn,
)
from settle_up.services import SettleUpClient, SettleUpWriteError
from settle_up.splitting import NothingToSplitError

router = Router(tags=["settle-up"])


@router.get("/groups/", response={200: list[SettleUpGroupSchema]})
@paginate
def get_settle_up_groups(request: HttpRequest) -> list[SettleUpGroup]:
    settle_up_client = SettleUpClient()
    return settle_up_client.get_groups()


@router.get("/users/", response={200: list[SettleUpUserSchema]})
@paginate
def get_settle_up_users(
    request: HttpRequest, group_id: GroupId
) -> list[SettleUpMember]:
    settle_up_client = SettleUpClient()
    return settle_up_client.get_group_members_by_group(group_id)


@router.post("/transactions/", response={204: None})
def post_settle_up_create_transaction(
    request: HttpRequest, payload: TransactionPostIn
) -> Status[None]:
    """File a receipt in its Settle Up group as one expense.

    204: Settle Up filed it. 422: no member owes anything, so there is nothing
    to split. 424: Settle Up refused it, so nothing was filed. 502: Settle Up
    failed, so it may or may not have been filed.
    """
    settle_up_client = SettleUpClient()
    try:
        settle_up_client.create_transaction(payload)
    except NothingToSplitError as error:
        raise HttpError(422, str(error)) from error
    except SettleUpWriteError as error:
        raise HttpError(424 if error.refused else 502, str(error)) from error

    return Status(204, None)
