import time

import requests
from django.conf import settings
from django.test import TestCase

from backend_api.serializer import UserTransactionSchema
from backend_api.settleup_utils import SettleUpClient


class TestTransaction(TestCase):

    def test_single_member_transaction(self):
        """
        Test single member transaction 10% tax excluded in total.
        """
        settle_up_client = SettleUpClient()
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="-O_65sO85eKnYhJ4mfUh",
                    cost=90,
                )
            ],
            tax_percentage=10,
            total_amount=99.0,
            group_id="-O_65sO713JKnTgmQpCt",
        )
        assert result == {'-O_65sO85eKnYhJ4mfUh': 99.0}

    def test_single_member_with_tax_transaction(self):
        """
        Test single member transaction 10% w/ tax included in total.
        """
        settle_up_client = SettleUpClient()
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="-O_65sO85eKnYhJ4mfUh",
                    cost=100,
                )
            ],
            tax_percentage=10,
            total_amount=100,
            group_id="-O_65sO713JKnTgmQpCt",
        )
        assert result == {'-O_65sO85eKnYhJ4mfUh': 100}

    def test_single_member_with_splits_with_tax_transaction(self):
        """
        Test single member transaction with 10% tax and 2 member splits included in total.
        """
        settle_up_client = SettleUpClient()
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="-O_65sO85eKnYhJ4mfUh",
                    cost=100,
                )
            ],
            tax_percentage=10,
            total_amount=200,
            split_receipt_items=[100],  # 50 per member + 5 (10% tax) = 55
            group_id="-O_65sO713JKnTgmQpCt",    # 2 member group
        )
        assert result == {'-O_65sO85eKnYhJ4mfUh': 150.0, '-O_65vk76uxDTYDTHDku': 50.0}

    def test_single_member_with_splits_without_tax_transaction(self):
        """
        Test single member transaction with 10% tax excluded in total and 2 member splits.
        """
        settle_up_client = SettleUpClient()
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="-O_65sO85eKnYhJ4mfUh",
                    cost=90,
                )
            ],
            tax_percentage=10,
            total_amount=198,
            split_receipt_items=[90],
            group_id="-O_65sO713JKnTgmQpCt",    # 2 member group
        )
        assert result == {'-O_65sO85eKnYhJ4mfUh': 148, '-O_65vk76uxDTYDTHDku': 49}

    def test_two_member_with_tax_transaction_without_split(self):
        settle_up_client = SettleUpClient()
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="-O_65sO85eKnYhJ4mfUh",
                    cost=100,
                ),
                UserTransactionSchema(
                    member_id="-O_65vk76uxDTYDTHDku",
                    cost=100,
                ),
            ],
            tax_percentage=10,
            total_amount=200,
            group_id="-O_65sO713JKnTgmQpCt",  # 2 member group
        )
        assert result == {'-O_65sO85eKnYhJ4mfUh': 100.0, '-O_65vk76uxDTYDTHDku': 100.0}

    def test_two_member_without_tax_transaction_without_split(self):
        settle_up_client = SettleUpClient()
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="-O_65sO85eKnYhJ4mfUh",
                    cost=90,
                ),
                UserTransactionSchema(
                    member_id="-O_65vk76uxDTYDTHDku",
                    cost=90,
                ),
            ],
            tax_percentage=10,
            total_amount=198,
            group_id="-O_65sO713JKnTgmQpCt",
        )
        assert result == {"-O_65sO85eKnYhJ4mfUh": 99.0, "-O_65vk76uxDTYDTHDku": 99.0}

    def test_two_member_without_tax_transaction_with_split(self):
        settle_up_client = SettleUpClient()
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="-O_65sO85eKnYhJ4mfUh",
                    cost=90,
                ),
                UserTransactionSchema(
                    member_id="-O_65vk76uxDTYDTHDku",
                    cost=90,
                ),
            ],
            tax_percentage=10,
            total_amount=297,
            split_receipt_items=[90],
            group_id="-O_65sO713JKnTgmQpCt",
        )
        assert result == {"-O_65sO85eKnYhJ4mfUh": 148, "-O_65vk76uxDTYDTHDku": 148}

    # def test_temp(self):
    #     settle_up_client = SettleUpClient()
    #
    #     total_amt = 1340
    #     paying_member_id = "-O_65sO85eKnYhJ4mfUh"
    #     group_id = "-O_65sO713JKnTgmQpCt"
    #     other_member_id = "-O_65vk76uxDTYDTHDku"
    #
    #     member_receipt_item_total_map = settle_up_client._compute_transaction(
    #         receipt_items=[
    #             UserTransactionSchema(
    #                 member_id=paying_member_id,
    #                 cost=464,
    #             ),
    #             UserTransactionSchema(
    #                 member_id=other_member_id,
    #                 cost=478,
    #             ),
    #             UserTransactionSchema(
    #                 member_id=other_member_id,
    #                 cost=277,
    #             ),
    #         ],
    #         tax_percentage=10,
    #         total_amount=total_amt,
    #         # split_receipt_items=[129],
    #         group_id=group_id,
    #     )
    #
    #     results = settle_up_client._compute_weights(tuple(member_receipt_item_total_map.values()))
    #     for_whom = [
    #         {"memberId": member_id, "weight": str(total_amt)}
    #         for member_id, total_amt in zip(
    #             member_receipt_item_total_map.keys(), results
    #         )
    #         if total_amt > 0
    #     ]
    #     now = time.time_ns() // 1_000_000
    #
    #     transaction_payload = {
    #         "currencyCode": "JPY",
    #         "dateTime": now,
    #         "exchangeRates": {"JPY": "1"},
    #         "fixedExchangeRate": False,
    #         "items": [{"amount": str(total_amt), "forWhom": for_whom}],
    #         # "receiptUrl": ""
    #         "purpose": "testing",
    #         "type": "expense",
    #         "whoPaid": [
    #             {
    #                 "memberId": paying_member_id,
    #                 "weight": str(total_amt),
    #             }
    #         ],
    #     }
    #
    #     response = requests.post(
    #         f"{settings.SETTLE_UP_BASE_URL}/transactions/{group_id}.json",
    #         json=transaction_payload,
    #         params=settle_up_client.auth_params,
    #     )
    #     print(response.status_code)
    #     print(response.json())
