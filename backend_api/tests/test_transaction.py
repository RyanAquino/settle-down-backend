from backend_api.serializer import UserTransactionSchema


class TestTransaction:
    """
    Test compute transaction logic.

    Total amount is always presumed as correct value from receipt
    """

    def test_single_member_transaction(self, settle_up_client):
        """
        Test a single member transaction 10% tax excluded in total.
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="Member 1",
                    cost=90,
                )
            ],
            tax_percentage=10,
            total_amount=99.0,
            group_id="Group A",
        )
        assert result == {"Member 1": 99.0}

    def test_single_member_with_tax_transaction(self, settle_up_client):
        """
        Test a single member transaction 10% w/ tax included in total.
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="Member 1",
                    cost=100,
                )
            ],
            tax_percentage=10,
            total_amount=100,
            group_id="Group A",
        )
        assert result == {"Member 1": 100}

    def test_single_member_with_splits_with_tax_transaction(self, settle_up_client):
        """
        Test a single member transaction with 10% tax and 2 member splits included in total.

        Scenario:
            Member 1 - paid an item for 100
            Another item for 100 which is shared among 2 members
            total of 200 with 10% tax rate
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="Member 1",
                    cost=100,
                )
            ],
            tax_percentage=10,
            total_amount=200,
            split_receipt_items=[100],  # 50 per member + 5(10% tax) = 55
            group_id="Group A",  # 2 member group
        )
        assert result == {"Member 1": 150.0, "Member 2": 50.0}

    def test_single_member_with_splits_without_tax_transaction(self, settle_up_client):
        """
        Test single member transaction with 10% tax excluded in total and 2 member splits.

        Scenario:
            Member 1 paid an item for 90
            Another item for 90 which is shared among 2 members
            Total of 198 with 10% tax rate
            Receipt items does not include tax yet
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="Member 1",
                    cost=90,
                )
            ],
            tax_percentage=10,
            total_amount=198,
            split_receipt_items=[90],
            group_id="Group A",  # 2 member group
        )
        assert result == {"Member 1": 148.5, "Member 2": 49.5}

    def test_two_member_with_tax_transaction_without_split(self, settle_up_client):
        """
        Test two member transaction with 10% tax included in total.
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="Member 1",
                    cost=100,
                ),
                UserTransactionSchema(
                    member_id="Member 2",
                    cost=100,
                ),
            ],
            tax_percentage=10,
            total_amount=200,
            group_id="Group A",  # 2 member group
        )
        assert result == {"Member 1": 100.0, "Member 2": 100.0}

    def test_two_member_without_tax_transaction_without_split(self, settle_up_client):
        """
        Test two member transaction with 10% tax excluded in total.
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="Member 1",
                    cost=90,
                ),
                UserTransactionSchema(
                    member_id="Member 2",
                    cost=90,
                ),
            ],
            tax_percentage=10,
            total_amount=198,
            group_id="Group A",
        )
        assert result == {"Member 1": 99.0, "Member 2": 99.0}

    def test_two_member_without_tax_transaction_with_split(self, settle_up_client):
        """
        Test two member transaction with 10% tax excluded in total and 2 member splits.
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(
                    member_id="Member 1",
                    cost=90,
                ),
                UserTransactionSchema(
                    member_id="Member 2",
                    cost=90,
                ),
            ],
            tax_percentage=10,
            total_amount=297,
            split_receipt_items=[90],
            group_id="Group A",
        )
        assert result == {"Member 1": 148.5, "Member 2": 148.5}

    def test_fractional_tax_balances_to_total(self, settle_up_client):
        """
        Test fractional tax is applied exactly and member shares sum to the total.

        Scenario:
            Member 1 - paid an item for 95
            Another item for 85 which is shared among 2 members
            total of 198 with 10% tax rate
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[UserTransactionSchema(member_id="Member 1", cost=95)],
            tax_percentage=10,
            total_amount=198,
            split_receipt_items=[85],
            group_id="Group A",
        )
        assert result == {"Member 1": 151.25, "Member 2": 46.75}

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
