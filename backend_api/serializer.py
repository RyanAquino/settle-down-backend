from django.contrib.auth.models import User
from pydantic import Field
from ninja import ModelSchema, Schema
from ninja.errors import HttpError
from pydantic import field_validator

from backend_api.dataclasses.receipt_item import ReceiptItemData
from backend_api.models import ReceiptItem, Receipt


class UserGetOutSchema(ModelSchema):
    class Meta:
        model = User
        exclude = [
            "password",
        ]

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

class ReceiptItemGetOut(ModelSchema):
    member_id: str | None = None

    class Meta:
        model = ReceiptItem
        fields = '__all__'

    @staticmethod
    def resolve_member_id(obj):
        if obj.owner:
            return obj.owner.username
        return None


class SettleUpGroupSchema(Schema):
    name: str
    id: str

class SettleUpUserSchema(Schema):
    name: str
    id: str


class ReceiptGetOut(ModelSchema):
    receipt_items: list[ReceiptItemGetOut]

    class Meta:
        model = Receipt
        fields = '__all__'

    @staticmethod
    def resolve_receipt_items(obj):
        return obj.receiptitem_set.all().order_by('item_order')


class TransactionPostIn(Schema):
    purpose: str
    total_amount: float
    tax_amount: float
    paying_member_id: str
    paying_member_total: float
    other_member_id: str
    other_member_total: float
    group_id: str


class OCRReceiptPostIn(Schema):
    paid_by_username: str

    @field_validator('paid_by_username')
    def validate_paid_by_username(cls, value):
        if not User.objects.filter(username=value).exists():
            raise HttpError(404, "User does not exist")
        return value


class OCRReceiptPostOut(Schema):
    receipt_items: list[ReceiptItemData]
    en_shop_name: str = Field(..., description="The name of the shop in the receipt in english")
    jp_shop_name: str = Field(..., description="The name of the shop in the receipt in japanese")
    tax_amount: float = Field(
        0, description="The tax amount of the receipt if applicable"
    )
    total_amount: float = Field(
        0, description="The total amount of all items in the receipt"
    )
