from datetime import datetime

from pydantic import BaseModel, Field
from django.utils import timezone


class ReceiptItemData(BaseModel):
    """
    Receipt Item model for each item in the receipt.
    """

    english_name: str = Field(
        description="The name of the purchased item in english in the receipt"
    )
    japanese_name: str = Field(
        description="The name of the purchased item in Japanese in the receipt"
    )
    item_order: int = Field(description="The order of the items in the receipt")
    cost: float = Field(description="The cost of the purchased item minus any discounts if applicable")
    quantity: int = Field(description="The quantity of the purchased item")
    discount: int = Field(0, description="The discounted value of the purchased item if any is provided")


class ReceiptData(BaseModel):
    receipt_items: list[ReceiptItemData]
    en_shop_name: str = Field(
        ..., description="The name of the shop in the receipt in english"
    )
    jp_shop_name: str = Field(
        ..., description="The name of the shop in the receipt in japanese"
    )
    tax_percentage: float = Field(
        0, description="The tax percentage in the receipt if applicable"
    )
    total_amount: float = Field(
        0, description="The total amount of all items in the receipt"
    )
    receipt_date: datetime = Field(
        default_factory=timezone.now, description="The date of the receipt"
    )
