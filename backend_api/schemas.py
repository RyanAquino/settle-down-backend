from datetime import datetime

from django.utils import timezone
from ninja import Schema
from pydantic import Field

from backend_api.dto.receipt_item import ReceiptItemData


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
    tax_percentage: int
    total_amount: float
    user_receipt_items: list[UserTransactionSchema]
    split_receipt_items: list[float] = Field(
        default_factory=list, description="Split receipt items"
    )
    group_id: str
    receipt_date: datetime | None = Field(
        None,
        description="The date/time of the receipt. Timezone-aware values are respected; naive values are interpreted in RECEIPT_TIMEZONE (default Asia/Tokyo)",
    )
    receipt_image_url: str | None = Field(
        None, description="The url of the uploaded receipt image"
    )


class OCRReceiptPostOut(Schema):
    receipt_items: list[ReceiptItemData]
    en_shop_name: str = Field(
        ..., description="The name of the shop translated to English"
    )
    jp_shop_name: str = Field(
        ...,
        description="The name of the shop as printed on the receipt, in its original language (Japanese, Chinese, or English)",
    )
    tax_percentage: float = Field(
        0,
        description="Tax percentage added on top of item costs (0 when the total already includes tax)",
    )
    total_amount: float = Field(
        0,
        description="The grand total exactly as printed on the receipt (items plus tax when tax_percentage > 0)",
    )
    receipt_date: datetime = Field(
        default_factory=timezone.now, description="The date of the receipt"
    )
    receipt_image_url: str = Field(
        ..., description="The url of the uploaded receipt image"
    )
