from datetime import datetime
from typing import Annotated

from ninja import Schema
from pydantic import Field, FiniteFloat


class SettleUpGroupSchema(Schema):
    name: str
    id: str
    currency: str | None = None


class SettleUpUserSchema(Schema):
    name: str
    id: str


# What a member owes for a line: a finite amount, never negative.
Cost = Annotated[FiniteFloat, Field(ge=0)]


class UserTransactionSchema(Schema):
    member_id: str
    cost: Cost


class TransactionPostIn(Schema):
    purpose: str
    paying_member_id: str
    tax_percentage: FiniteFloat
    total_amount: FiniteFloat
    user_receipt_items: list[UserTransactionSchema]
    split_receipt_items: list[Cost] = Field(
        default_factory=list, description="Split receipt items"
    )
    group_id: str
    receipt_date: datetime | None = Field(
        default=None,
        description=(
            "When the receipt was issued. An offset is honoured; a date without "
            "one is read as UTC. Defaults to now"
        ),
    )
    receipt_image_url: str | None = Field(
        default=None, description="The url of the uploaded receipt image"
    )
