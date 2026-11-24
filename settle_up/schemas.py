from datetime import datetime

from ninja import Schema
from pydantic import Field, FiniteFloat


class SettleUpGroupSchema(Schema):
    name: str
    id: str
    currency: str | None = None


class SettleUpUserSchema(Schema):
    name: str
    id: str


class UserTransactionSchema(Schema):
    member_id: str
    cost: float


class TransactionPostIn(Schema):
    purpose: str
    paying_member_id: str
    tax_percentage: FiniteFloat
    total_amount: float
    user_receipt_items: list[UserTransactionSchema]
    split_receipt_items: list[float] = Field(
        default_factory=list, description="Split receipt items"
    )
    group_id: str
    receipt_date: datetime | None = Field(
        default=None, description="The date of the receipt in Japan timezone"
    )
    receipt_image_url: str | None = Field(
        default=None, description="The url of the uploaded receipt image"
    )
