import time
from collections.abc import Sequence
from datetime import UTC
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from backend_api import firebase_auth
from backend_api.dto.settleup import SettleUpGroup, SettleUpMember
from backend_api.schemas import TransactionPostIn
from backend_api.utils import MemberCost, compute_member_totals, compute_weights


class SettleUpGroupNotFoundError(LookupError):
    """The requested Settle Up group does not exist (Firebase answered null)."""


class SettleUpClient:
    def __init__(self) -> None:
        cache_key = f"{settings.SETTLE_UP_USER}_token"

        creds: firebase_auth.FirebaseCredentials | None = cache.get(cache_key)
        if not creds:
            creds = firebase_auth.sign_in_with_email_and_password(
                settings.SETTLE_UP_API_KEY,
                settings.SETTLE_UP_USER,
                settings.SETTLE_UP_PASSWORD,
                timeout=settings.UPSTREAM_HTTP_TIMEOUT,
            )
            cache.set(cache_key, timeout=3500, value=creds)

        self.user_id = creds.get("localId")
        self.auth_params = {"auth": creds.get("idToken")}

    def _get(self, path: str) -> requests.Response:
        """GET a Settle Up REST resource, authorized and bounded by a timeout."""
        return requests.get(
            f"{settings.SETTLE_UP_BASE_URL}/{path}",
            params=self.auth_params,
            timeout=settings.UPSTREAM_HTTP_TIMEOUT,
        )

    def get_groups(self) -> list[SettleUpGroup]:
        # v2: the cached value's shape changed (currency field added); entries
        # pickled under the old key predate the attribute.
        cache_key = "settle_up_groups_v2"

        cached: list[SettleUpGroup] | None = cache.get(cache_key)
        if cached:
            return cached

        user_groups: dict[str, Any] = self._get(
            f"userGroups/{self.user_id}.json"
        ).json()
        groups_map = []

        for group_id in user_groups:
            # Via get_group so each document is individually cached too - a
            # later create_transaction then gets a warm hit for its currency.
            group = self._require_group(group_id)
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
        cache.set(cache_key, timeout=86500, value=groups_map)

        return groups_map

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        """Fetch (and cache) the raw Settle Up group document.

        Carries ``convertedToCurrency`` - the currency the group is configured
        in, which every transaction filed against it must use. Returns None for
        a nonexistent group (Firebase answers with null).
        """
        cache_key = f"{group_id}_settle_up_group"

        cached: dict[str, Any] | None = cache.get(cache_key)
        if cached:
            return cached

        group: dict[str, Any] | None = self._get(f"groups/{group_id}.json").json()
        # Firebase returns a truthy error body ({"error": ...}) on auth or
        # permission failures; caching it would pin the failure for ~24h.
        if isinstance(group, dict) and "name" in group:
            cache.set(cache_key, timeout=86500, value=group)

        return group

    def _require_group(self, group_id: str) -> dict[str, Any]:
        group = self.get_group(group_id)
        if group is None:
            raise SettleUpGroupNotFoundError(
                f"Settle Up group {group_id!r} does not exist"
            )
        return group

    def get_group_members_by_group(self, group_id: str) -> list[SettleUpMember]:
        cache_key = f"{group_id}_settle_up_users"

        cached: list[SettleUpMember] | None = cache.get(cache_key)
        if cached:
            return cached

        members: dict[str, dict[str, Any]] = self._get(
            f"members/{group_id}.json"
        ).json()
        result: list[SettleUpMember] = []

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
    def _compute_weights(shares: Sequence[float]) -> list[int]:
        return compute_weights(shares)

    def _compute_transaction(
        self,
        receipt_items: Sequence[MemberCost],
        tax_percentage: float,
        group_id: str,
        total_amount: float,
        split_receipt_items: Sequence[float] | None = None,
    ) -> dict[str, float]:
        members = self.get_group_members_by_group(group_id)
        return compute_member_totals(
            receipt_items=receipt_items,
            tax_percentage=tax_percentage,
            members=members,
            total_amount=total_amount,
            split_receipt_items=split_receipt_items,
        )

    def create_transaction(self, payload: TransactionPostIn) -> dict[str, Any]:
        if payload.receipt_date:
            receipt_date = payload.receipt_date.replace(tzinfo=UTC)
            date_time_ms = int(receipt_date.timestamp() * 1000)
        else:
            date_time_ms = time.time_ns() // 1_000_000

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
                member_receipt_item_total_map.keys(), results, strict=True
            )
            if total_amt > 0
        ]

        # Strict: never file money under a guessed currency. The identity
        # exchange rate is correct because the expense IS denominated in the
        # group's currency (Settle Up exposes no exchange-rates endpoint).
        currency = self._require_group(payload.group_id)["convertedToCurrency"]

        transaction_payload: dict[str, Any] = {
            "currencyCode": currency,
            "dateTime": date_time_ms,
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

        response = requests.post(
            f"{settings.SETTLE_UP_BASE_URL}/transactions/{payload.group_id}.json",
            json=transaction_payload,
            params=self.auth_params,
            timeout=settings.UPSTREAM_HTTP_TIMEOUT,
        )

        result: dict[str, Any] = response.json()
        return result
