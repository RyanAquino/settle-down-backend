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


# The scan response is the public contract the mobile app is built against. It
# is deliberately separate from the DTO the OCR model fills in, so rewording a
# prompt-facing field description never changes the API contract.


class OCRReceiptItemOut(Schema):
    english_name: str = Field(description="The item name in English")
    japanese_name: str = Field(description="The item name as printed on the receipt")
    item_order: int = Field(
        description="The position of the item on the receipt, from 1 in printed order"
    )
    cost: float = Field(
        description=(
            "What this line cost after its share of every discount, coupon, points "
            "and surcharge, for the quantity shown; before tax when tax is added on "
            "top. The printed price when the receipt did not reconcile"
        )
    )
    quantity: int = Field(description="The quantity purchased on this line")
    discount: float = Field(
        description=(
            "Everything taken off this line's printed price, its own discounts and "
            "its share of receipt-wide ones, already subtracted from cost"
        )
    )


class OCRReceiptPostOut(Schema):
    receipt_items: list[OCRReceiptItemOut] = Field(
        description="The purchased items, in printed order"
    )
    en_shop_name: str = Field(description="The shop name in English")
    jp_shop_name: str = Field(description="The shop name as printed on the receipt")
    tax_percentage: float = Field(
        description=(
            "The printed tax rate, or the one with the largest taxable base, or 0. "
            "When the costs add up to total_amount it is for display only; "
            "otherwise it is the rate added on top of the costs"
        )
    )
    total_amount: float = Field(
        description=(
            "What the group splits: the amount paid after every discount, coupon "
            "and points. The costs add up to it, or do with tax_percentage added"
        )
    )
    receipt_date: datetime = Field(description="The date of the receipt")
    receipt_image_url: str = Field(description="The url of the uploaded receipt image")
