"""The installed mobile app's receipt check, restated in Python.

The app (settle-down ``utils/receipt-preview.ts``, ``reconcileReceipt``) only
accepts a scan whose item costs add up to ``total_amount``, either directly or
with ``tax_percentage`` added and rounded down or up at the precision the
amounts are printed with. A scan it rejects is read once more and then ends at
"Retake photo". Asserting against this restatement shows what installed apps do
with a response without shipping an app release.

Kept line for line with the TypeScript, including its float tolerances.
"""

import math
from collections.abc import Sequence


def _fraction_digits(amount: float) -> int:
    # JavaScript's Number#toString: integers print without a fraction.
    text = repr(float(amount))
    if "e" in text or text.endswith(".0"):
        return 0
    return len(text.split(".")[1])


def accepts(
    costs: Sequence[float],
    tax_percentage: float,
    total: float,
    item_orders: Sequence[int],
) -> bool:
    subtotal = 0.0
    for cost in costs:
        subtotal += cost
    tax_amount = (subtotal * tax_percentage) / 100
    if (
        not math.isfinite(subtotal)
        or not math.isfinite(tax_percentage)
        or subtotal <= 0
        or total <= 0
        or tax_percentage < 0
        or any(cost < 0 for cost in costs)
        or len(set(item_orders)) != len(item_orders)
    ):
        return False

    def matches(amount: float) -> bool:
        return abs(total - amount) < 1e-7

    if matches(subtotal):
        return True

    visible = max(_fraction_digits(amount) for amount in [total, *costs])
    printed = 0 if visible == 0 else max(2, visible)
    scale = 10**printed
    scaled_tax = tax_amount * scale
    lower = math.floor(scaled_tax + 1e-7) / scale
    upper = math.ceil(scaled_tax - 1e-7) / scale
    return matches(subtotal + lower) or matches(subtotal + upper)
