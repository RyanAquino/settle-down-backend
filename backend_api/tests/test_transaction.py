import pytest

from backend_api.schemas import UserTransactionSchema


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

    def test_every_tax_term_rounds_to_cents(self, settle_up_client):
        """
        Each tax term is rounded to 2 decimals before the total is compared.

        At 8%, every term here has a third decimal (0.804, 1.608, 0.804), so
        rounding to any other precision changes the outcome: the tax-exclusive
        total no longer matches (tax would silently be dropped) or a member's
        share drifts. Pins the load-bearing round(_, 2) calls.

        Scenario:
            Member 1 - paid an item for 10.05
            Another item for 20.10 which is shared among 2 members
            total of 32.56 = 10.05 + 0.80 tax + 20.10 + 1.61 tax
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[UserTransactionSchema(member_id="Member 1", cost=10.05)],
            tax_percentage=8,
            total_amount=32.56,
            split_receipt_items=[20.10],
            group_id="Group A",
        )
        assert result == {
            "Member 1": pytest.approx(10.05 + 0.80 + 10.05 + 0.80, abs=1e-9),
            "Member 2": pytest.approx(10.05 + 0.80, abs=1e-9),
        }

    def test_member_items_are_summed_per_member(self, settle_up_client):
        """
        Test a member's items are added together, with 10% tax excluded in total.

        Scenario:
            Member 1 - paid two items, 100 and 50
            Member 2 - paid an item for 50
            total of 220 = 150 + 15 tax + 50 + 5 tax
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[
                UserTransactionSchema(member_id="Member 1", cost=100),
                UserTransactionSchema(member_id="Member 1", cost=50),
                UserTransactionSchema(member_id="Member 2", cost=50),
            ],
            tax_percentage=10,
            total_amount=220,
            group_id="Group A",
        )
        assert result == {"Member 1": 165.0, "Member 2": 55.0}

    def test_every_shared_item_counts_toward_the_tax_check(self, settle_up_client):
        """
        Test every shared item and its tax is included when deciding whether tax is excluded.

        Scenario:
            Member 1 - paid an item for 100
            Two items, 100 and 200, which are shared among 2 members
            total of 440 = 100 + 10 tax + 100 + 10 tax + 200 + 20 tax
        """
        result = settle_up_client._compute_transaction(
            receipt_items=[UserTransactionSchema(member_id="Member 1", cost=100)],
            tax_percentage=10,
            total_amount=440,
            split_receipt_items=[100, 200],  # 150 per member + 15 tax = 165
            group_id="Group A",
        )
        assert result == {"Member 1": 275.0, "Member 2": 165.0}

    def test_compute_weights_reduces_to_smallest_integer_ratio(self, settle_up_client):
        """_compute_weights divides scaled shares by their GCD."""
        assert settle_up_client._compute_weights((150.0, 50.0)) == [3, 1]
        assert settle_up_client._compute_weights((100.0, 100.0)) == [1, 1]
        assert settle_up_client._compute_weights((36, 64)) == [9, 16]

    def test_compute_weights_keeps_cent_precision(self, settle_up_client):
        """_compute_weights scales shares to cents, so the ratio is exact to the cent."""
        assert settle_up_client._compute_weights((12.34, 5.66)) == [617, 283]
