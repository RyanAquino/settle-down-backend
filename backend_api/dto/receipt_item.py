from datetime import datetime

from django.utils import timezone
from pydantic import BaseModel, Field


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
    cost: float = Field(
        description="The final line price for this item after any discount, for the quantity shown"
    )
    quantity: int = Field(description="The quantity of the purchased item")
    discount: int = Field(
        0,
        description="The discount amount applied to this line, in yen (0 if none). Already subtracted from cost",
    )


class ReceiptData(BaseModel):
    receipt_items: list[ReceiptItemData] = Field(
        default_factory=list, description="The list of items in the receipt"
    )
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
        0, description="The total amount of all items stated in the receipt"
    )
    receipt_date: datetime = Field(
        default_factory=timezone.now, description="The date of the receipt"
    )
