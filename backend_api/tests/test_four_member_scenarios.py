"""Four-member group scenarios, end-to-end through ``create_transaction``.

The client app (settle-down) assigns each quantity-1 item to exactly one
member (or marks it shared for the whole group), then submits one
``user_receipt_items`` entry per participating member plus the shared costs.
These tests tailor the mocked members endpoint to a four-member group and
assert on the payload actually POSTed to Settle Up.
"""

from backend_api.dto.receipt_item import ReceiptItemData
from backend_api.schemas import TransactionPostIn
from backend_api.utils import spread_item_quantities

FOUR_MEMBERS = {f"member_{n}": {"name": f"Member {n}"} for n in range(1, 5)}


def _posted_payload(mock_settleup):
    return mock_settleup.requests.post.call_args.kwargs["json"]


class TestFourMemberScenarios:
    def test_only_two_of_four_members_bought_items(
        self, settle_up_client, mock_settleup
    ):
        """Scenario: 2 of 4 members bought; a third paid. Non-buyers owe nothing."""
        mock_settleup.requests.get.return_value.json.return_value = FOUR_MEMBERS

        settle_up_client.create_transaction(
            TransactionPostIn(
                purpose="Dinner",
                paying_member_id="member_3",  # the payer bought nothing
                tax_percentage=0,
                total_amount=800,
                user_receipt_items=[
                    {"member_id": "member_1", "cost": 500},
                    {"member_id": "member_2", "cost": 300},
                ],
                split_receipt_items=[],
                group_id="group-1",
            )
        )

        posted = _posted_payload(mock_settleup)
        for_whom = {m["memberId"]: m["weight"] for m in posted["items"][0]["forWhom"]}
        # Only the two buyers owe, in a 5:3 ratio; members 3 and 4 are absent.
        assert for_whom == {"member_1": "5", "member_2": "3"}
        # The non-buying payer still fronts the full amount.
        assert posted["whoPaid"] == [{"memberId": "member_3", "weight": "800"}]

    def test_two_buyers_plus_shared_item_spreads_to_all_four(
        self, settle_up_client, mock_settleup
    ):
        """Scenario: 2 of 4 bought items, but a shared item pulls everyone in."""
        mock_settleup.requests.get.return_value.json.return_value = FOUR_MEMBERS

        settle_up_client.create_transaction(
            TransactionPostIn(
                purpose="Dinner",
                paying_member_id="member_1",
                tax_percentage=0,
                total_amount=1000,
                user_receipt_items=[
                    {"member_id": "member_1", "cost": 500},
                    {"member_id": "member_2", "cost": 300},
                ],
                split_receipt_items=[200],  # 50 per member
                group_id="group-1",
            )
        )

        posted = _posted_payload(mock_settleup)
        for_whom = {m["memberId"]: m["weight"] for m in posted["items"][0]["forWhom"]}
        # 550 : 350 : 50 : 50 reduces to 11 : 7 : 1 : 1.
        assert for_whom == {
            "member_1": "11",
            "member_2": "7",
            "member_3": "1",
            "member_4": "1",
        }

    def test_two_members_same_item_two_members_different_items(
        self, settle_up_client, mock_settleup
    ):
        """Scenario: a Shake x2 line is spread into two units claimed by two
        members; the other two members bought different items. Tax-exclusive
        10% receipt so the tax heuristic must also fire."""
        mock_settleup.requests.get.return_value.json.return_value = FOUR_MEMBERS

        shake_units = spread_item_quantities(
            [
                ReceiptItemData(
                    english_name="Shake",
                    japanese_name="シェイク",
                    item_order=1,
                    cost=700,
                    quantity=2,
                )
            ]
        )
        assert [u.cost for u in shake_units] == [350, 350]

        settle_up_client.create_transaction(
            TransactionPostIn(
                purpose="Dinner",
                paying_member_id="member_1",
                tax_percentage=10,
                total_amount=2640,  # (350 + 350 + 1200 + 500) * 1.1
                user_receipt_items=[
                    {"member_id": "member_1", "cost": shake_units[0].cost},
                    {"member_id": "member_2", "cost": shake_units[1].cost},
                    {"member_id": "member_3", "cost": 1200},
                    {"member_id": "member_4", "cost": 500},
                ],
                split_receipt_items=[],
                group_id="group-1",
            )
        )

        posted = _posted_payload(mock_settleup)
        for_whom = {m["memberId"]: m["weight"] for m in posted["items"][0]["forWhom"]}
        # With tax: 385 : 385 : 1320 : 550 reduces to 7 : 7 : 24 : 10 — the two
        # same-item members carry identical weights.
        assert for_whom == {
            "member_1": "7",
            "member_2": "7",
            "member_3": "24",
            "member_4": "10",
        }
        assert for_whom["member_1"] == for_whom["member_2"]
        assert posted["whoPaid"] == [{"memberId": "member_1", "weight": "2640"}]
