import math
import time
from datetime import timezone
from functools import reduce
from zoneinfo import ZoneInfo

import pyrebase
import requests
from black.trans import defaultdict
from django.conf import settings
from django.core.cache import cache

from backend_api.dataclasses.settleup import SettleUpGroup
from backend_api.serializer import TransactionPostIn, UserTransactionSchema


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
            cache.set(f"{settings.SETTLE_UP_USER}_token", timeout=3500, value=creds)

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
        """
        Convert member shares into integer weights.

        shares: list of member shares (e.g., [36, 64])
        returns: list of weights (e.g., [9, 16])
        """
        # Step 1: convert shares to integers if they aren't already
        scaled = [int(round(s * 100)) for s in shares]

        # Step 2: find GCD of all shares
        gcd_all = reduce(math.gcd, scaled)

        # Step 3: divide each share by the GCD to get weights
        weights = [s // gcd_all for s in scaled]

        return weights

    def _compute_transaction(
        self,
        receipt_items: list[UserTransactionSchema],
        tax_percentage: int,
        group_id: str,
        total_amount: float = 0,
        split_receipt_items: list[float] = None
    ):
        if split_receipt_items is None:
            split_receipt_items = []

        # Calculate tax
        member_receipt_item_total_map = defaultdict(float)
        member_receipt_tax_map = defaultdict(int)
        tax_percentage /= 100

        # Total cost per member
        for member in receipt_items:
            member_receipt_item_total_map[member.member_id] += member.cost

        # Tax per consolidated items member
        for member_id, item_amt in member_receipt_item_total_map.items():
            member_receipt_tax_map[member_id] += int(item_amt * tax_percentage)

        # Shared tax for total verification
        shared_tax = 0
        member_users = self.get_group_members_by_group(group_id)
        if split_receipt_items:
            for total_amt in split_receipt_items:
                shared_tax += (total_amt + int(total_amt * tax_percentage))

        should_compute_tax = sum([*member_receipt_tax_map.values() ,*member_receipt_item_total_map.values(), shared_tax]) == total_amount
        if should_compute_tax:
            for member_id in member_receipt_item_total_map.keys():
                member_receipt_item_total_map[member_id] += member_receipt_tax_map.get(member_id, 0)

        # Shared item split cost + tax if applicable
        for shared_item in split_receipt_items:
            for member in member_users:
                member_id = member["id"]
                portion_amt = shared_item / len(member_users)
                member_receipt_item_total_map[member_id] += portion_amt

                if should_compute_tax:
                    member_receipt_item_total_map[member_id] += int((portion_amt * tax_percentage))

        return member_receipt_item_total_map


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

        results = self._compute_weights(
            tuple(member_receipt_item_total_map.values())
        )
        for_whom = [{"memberId": member_id, "weight": str(total_amt)} for member_id, total_amt in zip(member_receipt_item_total_map.keys(), results) if total_amt > 0]

        transaction_payload = {
            "currencyCode": "JPY",
            "dateTime": now,
            "exchangeRates": {"JPY": "1"},
            "fixedExchangeRate": False,
            "items": [
                {
                    "amount": str(payload.total_amount),
                    "forWhom": for_whom
                }
            ],
            # "receiptUrl": ""
            "purpose": payload.purpose,
            "type": "expense",
            "whoPaid": [
                {
                    "memberId": payload.paying_member_id,
                    "weight": str(payload.total_amount),
                }
            ],
        }

        response = requests.post(
            f"{settings.SETTLE_UP_BASE_URL}/transactions/{payload.group_id}.json",
            json=transaction_payload,
            params=self.auth_params,
        )

        return response.json()
