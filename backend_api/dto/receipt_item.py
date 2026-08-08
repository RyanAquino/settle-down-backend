from datetime import datetime

from pydantic import BaseModel, Field


class ReceiptItemData(BaseModel):
    """
    Receipt Item model for each item in the receipt.

    ``japanese_name`` predates multi-region support and is kept for API
    compatibility: it holds the item name exactly as printed on the receipt,
    whatever the language (Japanese, Chinese, or English).
    """

    english_name: str = Field(
        description="The name of the purchased item translated to English"
    )
    japanese_name: str = Field(
        description="The name of the purchased item exactly as printed on the receipt, in its original language and script (Japanese, Chinese, or English) — never translated"
    )
    item_order: int = Field(description="The order of the items in the receipt")
    cost: float = Field(
        description="The final line price for this item after any discount, for the quantity shown"
    )
    quantity: int = Field(description="The quantity of the purchased item")
    discount: float = Field(
        0,
        description="The discount amount applied to this line, in the receipt's currency (0 if none). Already subtracted from cost",
    )


class ReceiptData(BaseModel):
    receipt_items: list[ReceiptItemData] = Field(
        default_factory=list, description="The list of items in the receipt"
    )
    en_shop_name: str = Field(
        ..., description="The name of the shop translated to English"
    )
    jp_shop_name: str = Field(
        ...,
        description="The name of the shop exactly as printed on the receipt, in its original language and script (Japanese, Chinese, or English) — never translated",
    )
    tax_percentage: float = Field(
        0,
        description="The tax rate (%) added on top of the item costs to reach total_amount; 0 when the printed total already includes tax",
    )
    total_amount: float = Field(
        0, description="The grand total exactly as printed on the receipt"
    )
    receipt_date: datetime | None = Field(
        None,
        description="The date of the receipt converted to the Gregorian calendar; null when the receipt shows no date",
    )
