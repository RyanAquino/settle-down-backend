"""Property-based tests for the receipt-splitting math in ``backend_api.utils``.

The example-based tests in ``test_transaction.py`` pin specific receipts; these
check invariants across thousands of generated receipts (more in the nightly
run, via ``--hypothesis-profile=nightly``).

The generators use whole-yen amounts in multiples that keep every tax term an
exact integer (10% of a multiple of 10, shared items divisible by the member
count). That keeps the load-bearing exact float ``==`` tax inference out of
the picture, so the invariants can be asserted exactly instead of approximately.
"""

import math
from functools import reduce
from itertools import combinations

from hypothesis import given
from hypothesis import strategies as st

from backend_api.dto.settleup import SettleUpMember
from backend_api.schemas import UserTransactionSchema
from backend_api.utils import compute_member_totals, compute_weights

TAX_PERCENTAGES = st.sampled_from([0, 10])


@st.composite
def receipts(draw):
    """A group, per-member itemized costs and evenly split shared items."""
    member_count = draw(st.integers(min_value=1, max_value=6))
    members: list[SettleUpMember] = [
        {"id": f"member-{i}", "name": f"Member {i}"} for i in range(member_count)
    ]
    items = draw(
        st.lists(
            st.builds(
                UserTransactionSchema,
                member_id=st.sampled_from([m["id"] for m in members]),
                cost=st.integers(min_value=0, max_value=5_000).map(lambda n: n * 10),
            ),
            max_size=12,
        )
    )
    shared = draw(
        st.lists(
            st.integers(min_value=1, max_value=500).map(
                lambda n: n * 10 * member_count
            ),
            max_size=4,
        )
    )
    return members, items, shared


def _pre_tax(items, shared):
    return sum(item.cost for item in items) + sum(shared)


def _base_share(member, items, shared, members):
    """What a member owes before tax: their own items plus an equal split."""
    own = sum(item.cost for item in items if item.member_id == member["id"])
    return own + sum(shared) // len(members)


@given(receipts(), TAX_PERCENTAGES)
def test_tax_inclusive_receipt_splits_to_its_total(receipt, tax_percentage):
    """A printed total that already includes tax is split without adding any."""
    members, items, shared = receipt
    total = _pre_tax(items, shared)

    owed = compute_member_totals(items, tax_percentage, members, total, shared)

    assert sum(owed.values()) == total
    for member in members:
        expected = _base_share(member, items, shared, members)
        assert owed.get(member["id"], 0) == expected


@given(receipts())
def test_tax_exclusive_receipt_adds_tax_to_everyone(receipt):
    """A printed total of items plus 10% tax charges each member their tax."""
    members, items, shared = receipt
    pre_tax = _pre_tax(items, shared)
    total = pre_tax + pre_tax // 10

    owed = compute_member_totals(items, 10, members, total, shared)

    assert sum(owed.values()) == total
    for member in members:
        base = _base_share(member, items, shared, members)
        assert owed.get(member["id"], 0) == base + base // 10


@given(receipts(), TAX_PERCENTAGES)
def test_only_members_with_a_share_owe_anything(receipt, tax_percentage):
    members, items, shared = receipt

    owed = compute_member_totals(
        items, tax_percentage, members, _pre_tax(items, shared), shared
    )

    expected = {item.member_id for item in items}
    if shared:
        expected |= {m["id"] for m in members}
    assert set(owed) == expected


@given(
    st.lists(
        st.integers(min_value=0, max_value=1_000_000).map(lambda n: n / 100),
        min_size=1,
        max_size=8,
    ).filter(any)
)
def test_weights_are_the_smallest_integer_ratio_of_the_shares(shares):
    weights = compute_weights(shares)
    scaled = [round(share * 100) for share in shares]

    assert all(w >= 0 for w in weights)
    assert reduce(math.gcd, weights) == 1
    # Same ratio as the shares: w_i / w_j == s_i / s_j for every pair.
    for i, j in combinations(range(len(shares)), 2):
        assert weights[i] * scaled[j] == weights[j] * scaled[i]
