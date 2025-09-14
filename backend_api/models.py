import uuid

from django.contrib.auth.models import AbstractUser, User
from django.db import models
from django.db.models import CharField, ForeignKey, CASCADE, FileField


class SettleUpGroup(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    group_id = models.CharField(max_length=255)

    class Meta:
        unique_together = ("user", "group_id")


def _get_file_destination(self, filename) -> str:
    extension = filename.split(".")[-1]
    filename = f"{uuid.uuid4()}.{extension}"
    return f"media/{filename}"


class Receipt(models.Model):
    shop_name = CharField(max_length=100)
    receipt_file = FileField(upload_to=_get_file_destination)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_by = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    tax_amount = models.DecimalField(decimal_places=2, max_digits=10, null=True, blank=True)
    total_amount = models.DecimalField(decimal_places=2, max_digits=10, null=True, blank=True)

class ReceiptItem(models.Model):
    en_name = models.CharField(max_length=100)
    jp_name = models.CharField(max_length=100)
    item_order = models.IntegerField(default=1)
    cost = models.DecimalField(decimal_places=2, max_digits=10)
    quantity = models.PositiveIntegerField()
    discount = models.DecimalField(decimal_places=2, max_digits=10)
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE)
    owner = ForeignKey(
        User,
        on_delete=CASCADE,
        null=True,
        blank=True,
    )
