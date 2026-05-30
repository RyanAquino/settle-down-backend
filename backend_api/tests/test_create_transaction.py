from backend_api.schemas import TransactionPostIn, UserTransactionSchema


class TestCreateTransactionPayload:
    """Pin the exact Settle Up transaction body that create_transaction POSTs.

    Uses the mocked two-member group (Member 1 / Member 2) from conftest.
    The split decision here: items (100 + 100) + tax (10 + 10) = 220 != total
    200, so tax is treated as already included and is NOT added. Weights reduce
    to 1:1, while whoPaid carries the full total_amount.
    """

    def test_builds_expected_payload(self, settle_up_client, mock_settleup):
        payload = TransactionPostIn(
            purpose="Lunch",
            paying_member_id="Member 1",
            tax_percentage=10,
            total_amount=200.0,
            user_receipt_items=[
                UserTransactionSchema(member_id="Member 1", cost=100),
                UserTransactionSchema(member_id="Member 2", cost=100),
            ],
            split_receipt_items=[],
            group_id="Group A",
        )

        settle_up_client.create_transaction(payload)

        body = mock_settleup.requests.post.call_args.kwargs["json"]
        assert body["currencyCode"] == "JPY"
        assert body["type"] == "expense"
        assert body["exchangeRates"] == {"JPY": "1"}
        assert body["fixedExchangeRate"] is False
        assert body["purpose"] == "Lunch"
        assert body["whoPaid"] == [{"memberId": "Member 1", "weight": "200.0"}]
        assert body["items"] == [
            {
                "amount": "200.0",
                "forWhom": [
                    {"memberId": "Member 1", "weight": "1"},
                    {"memberId": "Member 2", "weight": "1"},
                ],
            }
        ]
        assert "receiptUrl" not in body
        assert isinstance(body["dateTime"], int)

    def test_receipt_image_url_sets_receipturl(self, settle_up_client, mock_settleup):
        payload = TransactionPostIn(
            purpose="Lunch",
            paying_member_id="Member 1",
            tax_percentage=10,
            total_amount=200.0,
            user_receipt_items=[
                UserTransactionSchema(member_id="Member 1", cost=100),
                UserTransactionSchema(member_id="Member 2", cost=100),
            ],
            group_id="Group A",
            receipt_image_url="https://example.com/receipt.jpg",
        )

        settle_up_client.create_transaction(payload)

        body = mock_settleup.requests.post.call_args.kwargs["json"]
        assert body["receiptUrl"] == "https://example.com/receipt.jpg"
