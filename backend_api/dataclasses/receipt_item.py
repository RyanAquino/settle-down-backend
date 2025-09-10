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
    item_order: int = Field(
        description="The order of the items in the receipt"
    )
    cost: float = Field(description="The cost of the purchased item")
    quantity: int = Field(description="The quantity of the purchased item")
    discount: int = Field(0, description="The discounted value of the purchased item")


class ReceiptData(BaseModel):
    receipt_items: list[ReceiptItemData]
    shop_name: str = Field(
        ..., description="The name of the shop in the receipt"
    )
