from datetime import datetime

from django.utils import timezone
from ninja import Schema
from ninja.errors import HttpError
from pydantic import Field, model_validator
from pydantic import field_validator

from backend_api.dataclasses.receipt_item import ReceiptItemData


class UserFilterQuery(Schema):
    group_id: str | None = None


class UserPostIn(Schema):
    name: str


class UserPath(Schema):
    user_id: int


class ReceiptPath(Schema):
    receipt_id: int


class ReceiptItemPath(Schema):
    receipt_item_id: int


class ReceiptPatchIn(Schema):
    paid_by: str


class ReceiptItemPostIn(Schema):
    cost: float
    quantity: int
    discount: float
    owner_id: int


class SettleUpGroupSchema(Schema):
    name: str
    id: str


class SettleUpUserSchema(Schema):
    name: str
    id: str


class UserTransactionSchema(Schema):
    member_id: str
    cost: float


class TransactionPostIn(Schema):
    purpose: str
    paying_member_id: str
    tax_percentage: int
    total_amount: float
    user_receipt_items: list[UserTransactionSchema]
    split_receipt_items: list[float] = Field(default_factory=list, description="Split receipt items")
    group_id: str
    receipt_date: datetime | None = Field(None, description="The date of the receipt in Japan timezone")


class OCRReceiptPostIn(Schema):
    paid_by_username: str

    @field_validator("paid_by_username")
    def validate_paid_by_username(cls, value):
        if not User.objects.filter(username=value).exists():
            raise HttpError(404, "User does not exist")
        return value


class OCRReceiptPostOut(Schema):
    receipt_items: list[ReceiptItemData]
    en_shop_name: str = Field(
        ..., description="The name of the shop in the receipt in english"
    )
    jp_shop_name: str = Field(
        ..., description="The name of the shop in the receipt in japanese"
    )
    tax_percentage: float = Field(
        0, description="The tax percentage of the receipt if applicable"
    )
    total_amount: float = Field(
        0, description="The total amount of all items in the receipt"
    )
    receipt_date: datetime = Field(
        default_factory=timezone.now, description="The date of the receipt"
    )
