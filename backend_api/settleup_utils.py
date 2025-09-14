import math
import time
from functools import reduce

import pyrebase
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from backend_api.dataclasses.settleup import SettleUpGroup
from backend_api.models import SettleUpGroup as SettleUpGroupDB
from backend_api.serializer import TransactionPostIn
from django.core.cache import cache

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
            cache.set(
                f"{settings.SETTLE_UP_USER}_token",
                timeout=3500,
                value=creds
            )

        self.user_id = creds.get("localId")
        self.auth_params = {"auth": creds.get("idToken")}

    def get_groups(self) -> list[SettleUpGroup]:
        groups = requests.get(
            f"{settings.SETTLE_UP_BASE_URL}/userGroups/{self.user_id}.json",
            params=self.auth_params,
        )
        groups = groups.json()
        groups_map = []

        for group_id, metadata in groups.items():
            group = requests.get(f"{settings.SETTLE_UP_BASE_URL}/groups/{group_id}.json", params=self.auth_params)
            group = group.json()
            groups_map.append(
                SettleUpGroup(
                    name=group["name"],
                    id=group_id,
                )
            )

        return groups_map

    def get_group_members_by_group(self, group_id):
        members = requests.get(f"{settings.SETTLE_UP_BASE_URL}/members/{group_id}.json", params=self.auth_params)
        members = members.json()
        result = []

        for member_id, metadata in members.items():
            name = metadata.get("name")
            result.append({
                "id": member_id,
                "name": name,
            })

        return result

    @transaction.atomic
    def get_or_create_group_members(self, groups):
        for group in groups:
            members = requests.get(f"{settings.SETTLE_UP_BASE_URL}/members/{group.id}.json", params=self.auth_params)
            members = members.json()

            for member_id, metadata in members.items():
                name = metadata.get("name")
                user, _ = User.objects.get_or_create(username=member_id, first_name=name)
                SettleUpGroupDB.objects.get_or_create(
                    group_id=group.id,
                    user=user,
                )


    @staticmethod
    def _compute_weights(shares):
        """
        Convert member shares into integer weights.

        shares: list of member shares (e.g., [36, 64])
        returns: list of weights (e.g., [9, 16])
        """
        # Step 1: convert shares to integers if they aren't already
        shares = [int(round(s)) for s in shares]

        # Step 2: find GCD of all shares
        gcd_all = reduce(math.gcd, shares)

        # Step 3: divide each share by the GCD to get weights
        weights = [s // gcd_all for s in shares]

        return weights

    def create_transaction(
        self,
        payload: TransactionPostIn
    ):
        now = time.time_ns() // 1_000_000

        if payload.tax_amount != 0:
            tax_amount = payload.tax_amount / 2

        paying_member_total, other_member_total = self._compute_weights(
            (payload.paying_member_total + tax_amount,
            payload.other_member_total + tax_amount)
        )

        transaction_payload = {
            "currencyCode": "JPY",
            "dateTime": now,
            "exchangeRates": {"JPY": "1"},
            "fixedExchangeRate": False,
            "items": [
                {
                    "amount": str(payload.total_amount),
                    "forWhom": [
                        {
                            'memberId': payload.other_member_id,
                            'weight': str(other_member_total),
                        },
                        {
                            'memberId': payload.paying_member_id,
                            'weight': str(paying_member_total)
                        }
                    ],
                }
            ],
            # "receiptUrl": ""
            "purpose": payload.purpose,
            "type": "expense",
            "whoPaid": [{"memberId": payload.paying_member_id, "weight": str(payload.total_amount)}],
        }
        response = requests.post(
            f"{settings.SETTLE_UP_BASE_URL}/transactions/{payload.group_id}.json",
            json=transaction_payload,
            params=self.auth_params
        )

        return response.json()