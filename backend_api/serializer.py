from django.contrib.auth.models import User
from ninja import ModelSchema, Schema

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


class ReceiptGetOut(ModelSchema):
    receipt_items: list[ReceiptItemGetOut]

    class Meta:
        model = Receipt
        fields = '__all__'

    @staticmethod
    def resolve_receipt_items(obj):
        return obj.receiptitem_set.all().order_by('item_order')
