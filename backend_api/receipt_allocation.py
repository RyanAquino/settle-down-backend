"""Work out what each line of a transcribed receipt actually cost.

The OCR model only copies what is printed: line prices, every line that changes
the amount to pay, the tax lines and the amount due. This module does all of the
money math, in integer minor units, and never relies on the model's arithmetic.

1. Scope. Each adjustment applies to some of the items. Where the receipt prints
   a base amount (対象金額) or a percentage, the scope is resolved from those
   numbers; the items the model names are only a hint.
2. Allocation. Adjustments are applied in printed order. Each one is split over
   its items in proportion to what they cost at that point, by largest
   remainder in the currency's minor unit, ties to the lower item_order.
3. Reconciliation. The allocated items must add up to the printed amount due,
   directly, with one printed tax rate added on top, or with a printed tax
   amount that has no rate (spread over the items), and must agree with the
   printed per-rate taxable bases when those are printed.

A receipt that does not reconcile, or that has an adjustment whose scope cannot
be resolved, allocates nothing: its items keep their printed prices. A client
that checks the items against the total then rejects it instead of filing a
guessed split.

Pure: no Django, HTTP, cache or SDK imports (enforced by import-linter).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from math import ceil, floor
from typing import Literal

from backend_api.dto.receipt_scan import AdjustmentKind, ReceiptScan

ScopeStatus = Literal["verified", "assumed", "unresolved"]
ReceiptStatus = Literal["reconciled", "needs_review", "mismatch"]

MAX_DECIMAL_PLACES = 3

# Bound on the subset search that recovers a scope from a printed base amount.
# Past it the search gives up and the scope is left unresolved; the bound keeps
# a long receipt from blocking the request.
_MAX_SUBSET_SUMS = 20_000


class TranscriptionError(ValueError):
    """The transcription cannot be read as money, so the model should read it again."""


@dataclass(frozen=True)
class Item:
    item_order: int
    price: int


@dataclass(frozen=True)
class Adjustment:
    label: str
    kind: AdjustmentKind
    amount: int
    percentage: Fraction | None = None
    eligible_subtotal: int | None = None
    # Item orders the receipt ties this line to, as the model read it.
    item_orders: tuple[int, ...] = ()


@dataclass(frozen=True)
class TaxLine:
    rate: Fraction | None
    taxable_amount: int | None
    tax_amount: int | None


@dataclass(frozen=True)
class PrintedReceipt:
    """A receipt as printed, every amount in integer minor units."""

    items: tuple[Item, ...]
    adjustments: tuple[Adjustment, ...]
    tax_lines: tuple[TaxLine, ...]
    amount_due: int
    decimal_places: int


@dataclass(frozen=True)
class AllocatedAdjustment:
    adjustment: Adjustment
    status: ScopeStatus
    # The resolved scope, or the model's hint when the scope is unresolved.
    item_orders: tuple[int, ...]
    # What each item in the scope took, in minor units. Empty when unresolved.
    allocations: dict[int, int]


@dataclass(frozen=True)
class Allocation:
    status: ReceiptStatus
    # Per item_order, in printed order: the final cost when reconciled (before
    # tax when tax is added on top), otherwise the printed price.
    costs: dict[int, int]
    # What the group splits: the amount paid, after every discount and points.
    total: int
    # The rate a client adds on top when ``tax_added``; otherwise display only.
    tax_rate: Fraction | None
    tax_added: bool
    # Unexplained difference when the status is ``mismatch``, otherwise 0: the
    # amount due less the allocated items, or, when only the printed taxable
    # bases disagree, the bases less the total.
    difference: int
    adjustments: tuple[AllocatedAdjustment, ...]


# ----------------------------------------------------------------- units ----


def _fraction_digits(amount: float) -> int:
    exponent = Decimal(str(amount)).normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def decimal_places_for(declared: int, amounts: Iterable[float]) -> int:
    """The currency's decimal places: as transcribed, or more if an amount has more.

    The transcribed value matters when every printed amount happens to be
    whole (a 10.00 dollar receipt arrives as 10.0). An amount with more
    decimals than transcribed raises the count instead of failing the scan.
    """
    places = max([declared, *(_fraction_digits(amount) for amount in amounts)])
    if not 0 <= places <= MAX_DECIMAL_PLACES:
        raise TranscriptionError(
            f"decimal_places must be between 0 and {MAX_DECIMAL_PLACES}, "
            f"got {places}."
        )
    return places


def to_minor_units(amount: float, decimal_places: int) -> int:
    scaled = Decimal(str(amount)).scaleb(decimal_places)
    if scaled != scaled.to_integral_value():
        raise TranscriptionError(
            f"{amount} has more than {decimal_places} decimal places."
        )
    return int(scaled)


def from_minor_units(units: int, decimal_places: int) -> float:
    return float(Decimal(units).scaleb(-decimal_places))


def _percentage(value: float | None) -> Fraction | None:
    """A printed percent as an exact fraction, or None when it cannot be one."""
    if value is None or not 0 < value <= 100:
        return None
    return Fraction(Decimal(str(value)))


def _rate(value: float | None) -> Fraction | None:
    if value is None or value < 0:
        return None
    return Fraction(Decimal(str(value)))


# ------------------------------------------------------------ conversion ----


def printed_receipt(scan: ReceiptScan) -> PrintedReceipt:
    """Convert a transcription to minor units, repairing what code can repair.

    Raises ``TranscriptionError`` only for what the model has to read again:
    no items, a negative price, a non-positive amount due, or an impossible
    number of decimal places.
    """
    if not scan.items:
        raise TranscriptionError(
            "No purchased items found; re-read the image and list every priced item."
        )
    for item in scan.items:
        if item.printed_price < 0:
            raise TranscriptionError(
                f"Item '{item.japanese_name}' has a negative price. Prices are "
                "copied as printed on the item's own line; a discount is an adjustment."
            )
    if scan.amount_due <= 0:
        raise TranscriptionError(
            "amount_due must be the positive final amount the customer paid."
        )

    places = decimal_places_for(
        scan.decimal_places,
        [
            scan.amount_due,
            *(item.printed_price for item in scan.items),
            *(adjustment.amount for adjustment in scan.adjustments),
            *(
                adjustment.eligible_subtotal
                for adjustment in scan.adjustments
                if adjustment.eligible_subtotal is not None
            ),
            *(
                amount
                for line in scan.tax_lines
                for amount in (line.taxable_amount, line.tax_amount)
                if amount is not None
            ),
        ],
    )

    # Items are numbered by printed position. The model's own numbers only
    # translate its hints, and only when they identify each item uniquely.
    model_orders = [item.item_order for item in scan.items]
    positions = (
        {order: position for position, order in enumerate(model_orders, start=1)}
        if len(set(model_orders)) == len(model_orders)
        else {}
    )

    def minor(amount: float) -> int:
        return to_minor_units(amount, places)

    def positive_or_none(amount: float | None) -> int | None:
        if amount is None:
            return None
        units = abs(minor(amount))
        return units or None

    adjustments: list[Adjustment] = []
    for scanned in scan.adjustments:
        # The sign comes from the kind; a copied minus sign is not an error.
        amount = abs(minor(scanned.amount))
        if amount == 0:
            continue
        hints = dict.fromkeys(
            positions[order] for order in scanned.item_orders if order in positions
        )
        adjustments.append(
            Adjustment(
                label=scanned.printed_label,
                kind=scanned.kind,
                amount=amount,
                percentage=_percentage(scanned.percentage),
                eligible_subtotal=positive_or_none(scanned.eligible_subtotal),
                item_orders=tuple(hints),
            )
        )

    return PrintedReceipt(
        items=tuple(
            Item(item_order=position, price=minor(item.printed_price))
            for position, item in enumerate(scan.items, start=1)
        ),
        adjustments=tuple(adjustments),
        tax_lines=tuple(
            TaxLine(
                rate=_rate(line.rate),
                taxable_amount=positive_or_none(line.taxable_amount),
                tax_amount=positive_or_none(line.tax_amount),
            )
            for line in scan.tax_lines
        ),
        amount_due=minor(scan.amount_due),
        decimal_places=places,
    )


# ------------------------------------------------------------ allocation ----


def split_proportionally(amount: int, weights: Mapping[int, int]) -> dict[int, int]:
    """Split ``amount`` over ``weights`` by largest remainder, in whole units.

    Remainders are compared as exact integers, and ties go to the lower key,
    so the split never depends on floating-point noise.
    """
    total = sum(weights.values())
    shares = {key: amount * weight // total for key, weight in weights.items()}
    leftover = amount - sum(shares.values())
    ranked = sorted(weights, key=lambda key: (-(amount * weights[key] % total), key))
    for key in ranked[:leftover]:
        shares[key] += 1
    return shares


def _percent_fits(percentage: Fraction, base: int, amount: int) -> bool:
    """``amount`` is ``percentage`` of ``base``, rounded any way (less than a unit off)."""
    return abs(percentage * base - 100 * amount) < 100


def _percent_fits_lines(
    percentage: Fraction, bases: Sequence[int], amount: int
) -> bool:
    """``amount`` is ``percentage`` of these lines, rounded on their sum or line by line."""
    if _percent_fits(percentage, sum(bases), amount):
        return True
    low = sum(floor(percentage * base / 100) for base in bases)
    high = sum(ceil(percentage * base / 100) for base in bases)
    return low <= amount <= high


def _unique_subset(
    orders: Sequence[int], weights: Mapping[int, int], target: int
) -> tuple[int, ...] | None:
    """The one subset of ``orders`` whose weights sum to ``target``, if exactly one."""
    # Each reachable sum maps to its number of subsets (capped at 2) and, while
    # that number is 1, a bitmask of the subset's positions in ``orders``.
    reachable: dict[int, tuple[int, int]] = {0: (1, 0)}
    for bit, order in enumerate(orders):
        weight = weights[order]
        for subtotal, (count, mask) in list(reachable.items()):
            reached = subtotal + weight
            if reached > target:
                continue
            seen, _ = reachable.get(reached, (0, 0))
            reachable[reached] = (min(2, seen + count), mask | 1 << bit)
        if len(reachable) > _MAX_SUBSET_SUMS:
            return None
    count, mask = reachable.get(target, (0, 0))
    if count != 1:
        return None
    return tuple(order for bit, order in enumerate(orders) if mask >> bit & 1)


def _scope_from_base(
    base: int,
    candidates: Sequence[tuple[int, ...]],
    live: tuple[int, ...],
    nets: Mapping[int, int],
) -> tuple[int, ...] | None:
    """The items whose current costs add up to the printed base amount.

    Current costs, not printed prices: the base is what the line was computed
    on at its place in the receipt, so a base that only matches printed prices
    means an earlier line was transcribed out of order.
    """
    for candidate in candidates:
        if candidate and sum(nets[order] for order in candidate) == base:
            return candidate
    return _unique_subset(live, nets, base)


def _single_item_fit(
    percentage: Fraction,
    amount: int,
    live: tuple[int, ...],
    nets: Mapping[int, int],
    prices: Mapping[int, int],
) -> tuple[int, ...] | None:
    """The one item the percentage fits, on printed prices first, then on nets.

    Two items at the same printed price can both fit; what they cost now (one
    already discounted, one not) can still tell them apart.
    """
    for basis in (prices, nets):
        fits = [
            order for order in live if _percent_fits(percentage, basis[order], amount)
        ]
        if len(fits) == 1:
            return (fits[0],)
    return None


def _scope_from_percentage(
    percentage: Fraction,
    amount: int,
    hint: tuple[int, ...],
    live: tuple[int, ...],
    unspecific: tuple[int, ...],
    nets: Mapping[int, int],
    prices: Mapping[int, int],
    item_specific: set[int],
) -> tuple[int, ...] | None:
    if len(hint) == 1:
        (named,) = hint
        on_price = _percent_fits(percentage, prices[named], amount)
        if on_price or _percent_fits(percentage, nets[named], amount):
            # The named item is doubtful when another item fits on its printed
            # price and the named one either fits only after an earlier
            # deduction or already has a discount of its own: that is the
            # misread of two item discounts both placed under one item.
            rivals = [
                order
                for order in live
                if order != named and _percent_fits(percentage, prices[order], amount)
            ]
            if rivals and (not on_price or named in item_specific):
                return None
            return hint
    elif hint and any(
        _percent_fits_lines(percentage, [basis[order] for order in hint], amount)
        for basis in (prices, nets)
    ):
        return hint
    if (single := _single_item_fit(percentage, amount, live, nets, prices)) is not None:
        return single
    for candidate in (live, unspecific):
        if candidate and _percent_fits_lines(
            percentage, [nets[order] for order in candidate], amount
        ):
            return candidate
    return None


def _resolve_scope(
    adjustment: Adjustment,
    nets: Mapping[int, int],
    prices: Mapping[int, int],
    item_specific: set[int],
) -> tuple[ScopeStatus, tuple[int, ...]]:
    live = tuple(order for order, net in nets.items() if net > 0)
    hint = tuple(order for order in adjustment.item_orders if nets.get(order, 0) > 0)
    unspecific = tuple(order for order in live if order not in item_specific)

    scope: tuple[int, ...] | None
    status: ScopeStatus = "verified"
    if adjustment.eligible_subtotal is not None:
        base = adjustment.eligible_subtotal
        scope = _scope_from_base(base, (hint, live, unspecific), live, nets)
        if (
            scope
            and adjustment.percentage is not None
            and not _percent_fits_lines(
                adjustment.percentage,
                [nets[order] for order in scope],
                adjustment.amount,
            )
        ):
            # The base and the percent disagree: one of them was misread.
            scope = None
    elif adjustment.percentage is not None:
        scope = _scope_from_percentage(
            adjustment.percentage,
            adjustment.amount,
            hint,
            live,
            unspecific,
            nets,
            prices,
            item_specific,
        )
    else:
        scope = hint or live
        if len(live) > 1:
            status = "assumed"

    if not scope:
        return "unresolved", adjustment.item_orders
    if adjustment.kind != "surcharge" and adjustment.amount > sum(
        nets[order] for order in scope
    ):
        return "unresolved", adjustment.item_orders
    return status, scope


def _apply(
    adjustment: Adjustment, scope: tuple[int, ...], nets: dict[int, int]
) -> dict[int, int]:
    allocations = split_proportionally(
        adjustment.amount, {order: nets[order] for order in scope}
    )
    sign = 1 if adjustment.kind == "surcharge" else -1
    for order, share in allocations.items():
        nets[order] += sign * share
    return allocations


# -------------------------------------------------------- reconciliation ----


def _display_rate(tax_lines: Sequence[TaxLine]) -> Fraction | None:
    """The rate to show: the only printed rate, or the one with the largest base."""
    rated = [line for line in tax_lines if line.rate is not None]
    if len({line.rate for line in rated}) == 1:
        return rated[0].rate
    with_base = [line for line in rated if line.taxable_amount is not None]
    if not with_base:
        return None
    return max(with_base, key=lambda line: line.taxable_amount or 0).rate


def _bases_disagree(
    tax_lines: Sequence[TaxLine], subtotal: int, total: int, points: int
) -> int:
    """How far the printed taxable bases are from this receipt's totals, or 0.

    Checked only when every rated tax line prints its base. Tax-inclusive bases
    add up to the amount paid, or to the amount before points when points were
    a payment; tax-exclusive bases add up to the items before tax.
    """
    rated = [line for line in tax_lines if line.rate is not None]
    if not rated or any(line.taxable_amount is None for line in rated):
        return 0
    bases = sum(line.taxable_amount or 0 for line in rated)
    if bases in (total, total + points, subtotal):
        return 0
    if all(line.tax_amount is not None for line in rated):
        tax = sum(line.tax_amount or 0 for line in rated)
        if bases + tax in (total, total + points):
            return 0
    return bases - total


def allocate(
    receipt: PrintedReceipt, scopes: Sequence[tuple[int, ...]] | None = None
) -> Allocation:
    """Allocate every adjustment and reconcile the result with the printed totals.

    ``scopes`` replaces scope resolution with one chosen scope per adjustment,
    for a person correcting the receipt. Each chosen scope must name existing
    items that can absorb the adjustment, or ``ValueError`` is raised.
    """
    if scopes is not None and len(scopes) != len(receipt.adjustments):
        raise ValueError("Choose exactly one scope for every adjustment.")
    prices = {item.item_order: item.price for item in receipt.items}
    nets = dict(prices)
    item_specific: set[int] = set()
    allocated: list[AllocatedAdjustment] = []

    for index, adjustment in enumerate(receipt.adjustments):
        status: ScopeStatus
        if scopes is None:
            status, scope = _resolve_scope(adjustment, nets, prices, item_specific)
        else:
            status, scope = "verified", _chosen_scope(adjustment, scopes[index], nets)
        if status == "unresolved":
            allocated.append(AllocatedAdjustment(adjustment, status, scope, {}))
            continue
        live = {order for order, net in nets.items() if net > 0}
        if adjustment.kind == "discount" and set(scope) < live:
            # A discount on some of the items (one item, a bundle, a subset):
            # a later "everything else" line excludes them.
            item_specific.update(scope)
        allocations = _apply(adjustment, scope, nets)
        allocated.append(AllocatedAdjustment(adjustment, status, scope, allocations))

    display_rate = _display_rate(receipt.tax_lines)
    if any(entry.status == "unresolved" for entry in allocated):
        return Allocation(
            status="needs_review",
            costs=dict(prices),
            total=receipt.amount_due,
            tax_rate=display_rate,
            tax_added=False,
            difference=0,
            adjustments=tuple(allocated),
        )

    subtotal = sum(nets.values())
    due = receipt.amount_due
    points = sum(
        adjustment.amount
        for adjustment in receipt.adjustments
        if adjustment.kind == "points"
    )
    rates = sorted({line.rate for line in receipt.tax_lines if line.rate is not None})
    unrated_tax = sum(line.tax_amount or 0 for line in receipt.tax_lines)

    total: int | None = None
    tax_added = False
    if subtotal == due:
        total = due
    elif len(rates) == 1 and due - subtotal in _added_tax(subtotal, rates[0]):
        # One printed rate added on top: the costs stay before tax and the
        # client adds the rate, exactly as it does for such receipts today.
        total, tax_added, display_rate = due, True, rates[0]
    elif not rates and unrated_tax and subtotal and subtotal + unrated_tax == due:
        # Tax added on top with no printed rate: no rate for the client to add,
        # so the printed tax goes into the costs in proportion. With a printed
        # rate that does not fit, some items are taxed differently and this
        # would misprice them, so that case stays a mismatch.
        for order, share in split_proportionally(unrated_tax, nets).items():
            nets[order] += share
        total = due
    elif points and subtotal == due - points:
        # The model copied the total printed before the points were taken off.
        total = due - points

    if total is None:
        difference = due - subtotal
    else:
        difference = _bases_disagree(receipt.tax_lines, subtotal, total, points)
    if total is None or difference:
        return Allocation(
            status="mismatch",
            costs=dict(prices),
            total=due,
            tax_rate=display_rate,
            tax_added=False,
            difference=difference,
            adjustments=tuple(allocated),
        )
    return Allocation(
        status="reconciled",
        costs=nets,
        total=total,
        tax_rate=display_rate,
        tax_added=tax_added,
        difference=0,
        adjustments=tuple(allocated),
    )


def _added_tax(subtotal: int, rate: Fraction) -> tuple[int, int]:
    """Tax on ``subtotal`` rounded down and up, as the mobile app accepts it."""
    exact = subtotal * rate / 100
    return floor(exact), ceil(exact)


def _chosen_scope(
    adjustment: Adjustment, chosen: tuple[int, ...], nets: Mapping[int, int]
) -> tuple[int, ...]:
    scope = tuple(dict.fromkeys(chosen))
    if not scope or any(order not in nets for order in scope):
        raise ValueError(f"'{adjustment.label}' must apply to items on the receipt.")
    available = sum(nets[order] for order in scope)
    if available == 0 or (
        adjustment.kind != "surcharge" and adjustment.amount > available
    ):
        raise ValueError(
            f"'{adjustment.label}' is larger than what its items still cost."
        )
    return scope


def allocate_scan(scan: ReceiptScan) -> tuple[PrintedReceipt, Allocation]:
    """Convert a transcription and allocate it."""
    receipt = printed_receipt(scan)
    return receipt, allocate(receipt)


def corrected_receipt(
    decimal_places: int,
    items: Sequence[tuple[int, float]],
    adjustments: Sequence[
        tuple[str, AdjustmentKind, float, float | None, float | None]
    ],
    tax_lines: Sequence[tuple[float | None, float | None, float | None]],
    amount_due: float,
) -> PrintedReceipt:
    """A receipt a person corrected in the app, for ``allocate(..., scopes=...)``.

    ``items`` are ``(item_order, printed price)``, ``adjustments`` are
    ``(label, kind, amount, percentage, eligible subtotal)`` and ``tax_lines``
    are ``(rate, taxable amount, tax amount)``, as the scan response returned
    them. The percentage and base are kept for display; chosen scopes decide
    the allocation. Raises ``ValueError`` for anything that cannot be money.
    """
    if not items:
        raise ValueError("A receipt needs at least one item.")
    orders = [order for order, _ in items]
    if len(set(orders)) != len(orders):
        raise ValueError("Each item needs its own item_order.")
    if any(price < 0 for _, price in items):
        raise ValueError("Item prices cannot be negative.")
    if any(adjustment[2] <= 0 for adjustment in adjustments):
        raise ValueError("Adjustment amounts must be positive.")
    if amount_due <= 0:
        raise ValueError("The total must be positive.")
    places = decimal_places_for(
        decimal_places,
        [
            amount_due,
            *(price for _, price in items),
            *(adjustment[2] for adjustment in adjustments),
            *(adjustment[4] for adjustment in adjustments if adjustment[4] is not None),
            *(
                amount
                for line in tax_lines
                for amount in line[1:]
                if amount is not None
            ),
        ],
    )

    def positive_or_none(amount: float | None) -> int | None:
        if amount is None:
            return None
        return abs(to_minor_units(amount, places)) or None

    return PrintedReceipt(
        items=tuple(
            Item(item_order=order, price=to_minor_units(price, places))
            for order, price in items
        ),
        adjustments=tuple(
            Adjustment(
                label=label,
                kind=kind,
                amount=to_minor_units(amount, places),
                percentage=_percentage(percentage),
                eligible_subtotal=positive_or_none(base),
            )
            for label, kind, amount, percentage, base in adjustments
        ),
        tax_lines=tuple(
            TaxLine(
                rate=_rate(rate),
                taxable_amount=positive_or_none(base),
                tax_amount=positive_or_none(tax),
            )
            for rate, base, tax in tax_lines
        ),
        amount_due=to_minor_units(amount_due, places),
        decimal_places=places,
    )
