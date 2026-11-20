import time
from datetime import timezone
from urllib.parse import quote

import requests
from django.conf import settings
from ninja.errors import HttpError

from backend_api.dto.settleup import SettleUpGroup
from backend_api.utils import compute_member_totals, compute_weights
from backend_api.schemas import TransactionPostIn, UserTransactionSchema


def group_key(value):
    if not value or any(
        char in ".#$[]/" or ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise HttpError(400, "Invalid group ID.")
    return quote(value, safe="")


class SettleUpClient:
    def __init__(self, uid, token):
        self.user_id = uid
        self.auth_params = {"auth": token}

    def _read(self, path):
        try:
            response = requests.get(
                f"{settings.SETTLE_UP_BASE_URL}/{path}.json",
                params=self.auth_params,
                timeout=15,
            )
        except requests.RequestException:
            raise HttpError(502, "Settle Up is temporarily unavailable.") from None
        self._check(response)
        return response.json()

    @staticmethod
    def _check(response):
        if response.status_code in (401, 403):
            raise HttpError(403, "Settle Up denied access.")
        if not response.ok:
            raise HttpError(502, "Settle Up could not complete the request.")

    def get_groups(self) -> list[SettleUpGroup]:
        groups = self._read(f"userGroups/{self.user_id}") or {}
        groups_map = []

        for group_id in groups:
            group = self.get_group(group_id)
            groups_map.append(
                SettleUpGroup(
                    name=group["name"],
                    id=group_id,
                    # Tolerant .get(): one malformed group must not break the
                    # whole listing. Strictness lives at transaction time.
                    currency=group.get("convertedToCurrency"),
                )
            )
        groups_map = groups_map[::-1]

        return groups_map

    def get_group(self, group_id) -> dict | None:
        """Fetch the raw Settle Up group document.

        Carries ``convertedToCurrency`` — the currency the group is configured
        in, which every transaction filed against it must use. Returns None for
        a nonexistent group (Firebase answers with null).
        """
        return self._read(f"groups/{group_key(group_id)}")

    def get_group_members_by_group(self, group_id):
        members = self._read(f"members/{group_key(group_id)}") or {}
        result = []

        for member_id, metadata in members.items():
            name = metadata.get("name")
            result.append(
                {
                    "id": member_id,
                    "name": name,
                }
            )

        return result

    @staticmethod
    def _compute_weights(shares):
        return compute_weights(shares)

    def _compute_transaction(
        self,
        receipt_items: list[UserTransactionSchema],
        tax_percentage: int,
        group_id: str,
        total_amount: float = 0,
        split_receipt_items: list[float] | None = None,
    ):
        members = self.get_group_members_by_group(group_id)
        return compute_member_totals(
            receipt_items=receipt_items,
            tax_percentage=tax_percentage,
            members=members,
            total_amount=total_amount,
            split_receipt_items=split_receipt_items,
        )

    def create_transaction(self, payload: TransactionPostIn):
        now = time.time_ns() // 1_000_000

        if payload.receipt_date:
            now = payload.receipt_date.replace(tzinfo=timezone.utc)
            now = int(now.timestamp() * 1000)

        member_receipt_item_total_map = self._compute_transaction(
            receipt_items=payload.user_receipt_items,
            tax_percentage=payload.tax_percentage,
            split_receipt_items=payload.split_receipt_items,
            group_id=payload.group_id,
            total_amount=payload.total_amount,
        )

        results = self._compute_weights(tuple(member_receipt_item_total_map.values()))
        for_whom = [
            {"memberId": member_id, "weight": str(total_amt)}
            for member_id, total_amt in zip(
                member_receipt_item_total_map.keys(), results
            )
            if total_amt > 0
        ]

        # Strict: never file money under a guessed currency. The identity
        # exchange rate is correct because the expense IS denominated in the
        # group's currency (Settle Up exposes no exchange-rates endpoint).
        currency = self.get_group(payload.group_id)["convertedToCurrency"]

        transaction_payload = {
            "currencyCode": currency,
            "dateTime": now,
            "exchangeRates": {currency: "1"},
            "fixedExchangeRate": False,
            "items": [{"amount": str(payload.total_amount), "forWhom": for_whom}],
            "purpose": payload.purpose,
            "type": "expense",
            "whoPaid": [
                {
                    "memberId": payload.paying_member_id,
                    "weight": str(payload.total_amount),
                }
            ],
        }

        if v := payload.receipt_image_url:
            transaction_payload["receiptUrl"] = v

        try:
            response = requests.post(
                f"{settings.SETTLE_UP_BASE_URL}/transactions/{group_key(payload.group_id)}.json",
                json=transaction_payload,
                params=self.auth_params,
                timeout=15,
            )

        except requests.RequestException:
            raise HttpError(502, "Settle Up is temporarily unavailable.") from None
        self._check(response)
        return response.json()
