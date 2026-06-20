import time
from datetime import timezone

import pyrebase
import requests
from django.conf import settings
from django.core.cache import cache

from backend_api.dataclasses.settleup import SettleUpGroup
from backend_api.utils import compute_member_totals, compute_weights
from backend_api.schemas import TransactionPostIn, UserTransactionSchema


class SettleUpClient:
    def __init__(self):
        firebase = pyrebase.initialize_app(settings.SETTLE_UP_CONFIG)
        pb_auth = firebase.auth()
        cache_key = f"{settings.SETTLE_UP_USER}_token"

        if v := cache.get(cache_key):
            creds = v
        else:
            creds = pb_auth.sign_in_with_email_and_password(
                settings.SETTLE_UP_USER,
                settings.SETTLE_UP_PASSWORD,
            )
            cache.set(cache_key, timeout=3500, value=creds)

        self.user_id = creds.get("localId")
        self.auth_params = {"auth": creds.get("idToken")}

    def get_groups(self) -> list[SettleUpGroup]:
        cache_key = "settle_up_groups"

        if v := cache.get(cache_key):
            return v

        groups = requests.get(
            f"{settings.SETTLE_UP_BASE_URL}/userGroups/{self.user_id}.json",
            params=self.auth_params,
        )
        groups = groups.json()
        groups_map = []

        for group_id, metadata in groups.items():
            group = requests.get(
                f"{settings.SETTLE_UP_BASE_URL}/groups/{group_id}.json",
                params=self.auth_params,
            )
            group = group.json()
            groups_map.append(
                SettleUpGroup(
                    name=group["name"],
                    id=group_id,
                )
            )
        groups_map = groups_map[::-1]
        cache.set(cache_key, timeout=86500, value=groups_map)

        return groups_map

    def get_group_members_by_group(self, group_id):
        cache_key = f"{group_id}_settle_up_users"

        if v := cache.get(cache_key):
            return v

        members = requests.get(
            f"{settings.SETTLE_UP_BASE_URL}/members/{group_id}.json",
            params=self.auth_params,
        )
        members = members.json()
        result = []

        for member_id, metadata in members.items():
            name = metadata.get("name")
            result.append(
                {
                    "id": member_id,
                    "name": name,
                }
            )

        cache.set(cache_key, timeout=86500, value=result)

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

        transaction_payload = {
            "currencyCode": "JPY",
            "dateTime": now,
            "exchangeRates": {"JPY": "1"},
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

        response = requests.post(
            f"{settings.SETTLE_UP_BASE_URL}/transactions/{payload.group_id}.json",
            json=transaction_payload,
            params=self.auth_params,
        )

        return response.json()
