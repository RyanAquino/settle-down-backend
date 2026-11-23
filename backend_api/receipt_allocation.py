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
3. Reconciliation. The allocated items must add up to the printed amount due:
   directly, with one printed tax rate added on top, or with printed tax that
   has no rate or that the printed bases tie to their items (spread into those
   items' costs). The printed tax lines must not contradict whether the prices
   include tax, and the printed per-rate taxable bases must agree, in sum and,
   when prices include tax, item by item.

A receipt that does not reconcile, or that has an adjustment whose scope cannot
be resolved, allocates nothing: every cost is 0. A client that checks the items
against the total then always rejects it instead of filing a guessed split;
printed prices could add up to a misread total.

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
# Sizes no real receipt reaches. Past them a receipt is not read at all, which
# keeps every search below bounded and every sum a finite float.
MAX_ITEMS = 500
MAX_ADJUSTMENTS = 100
MAX_TAX_LINES = 20
MAX_AMOUNT = 10**12  # minor units

# Work one allocation may spend on exact searches over sets of items: one step
# per reachable sum per item, shared by every search in the allocation so that
# many lines cannot multiply it. A search past it gives up, which fails closed
# (the scope stays unresolved, or the receipt does not reconcile).
_SEARCH_STEPS = 1_000_000
# Largest printed amount the per-rate grouping check searches against; a
# larger receipt skips that check.
_MAX_GROUP_TARGET = 5_000_000


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
    # tax when tax is added on top), otherwise 0, so that the costs never look
    # reconciled to a client that checks them against the total.
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
    One decimal place is kept in hundredths, the precision the app checks
    added tax at.
    """
    places = max([declared, *(_fraction_digits(amount) for amount in amounts)])
    if places == 1:
        places = 2
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
    if abs(scaled) > MAX_AMOUNT:
        raise TranscriptionError(f"{amount} is larger than any receipt amount.")
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
    if (
        len(scan.items) > MAX_ITEMS
        or len(scan.adjustments) > MAX_ADJUSTMENTS
        or len(scan.tax_lines) > MAX_TAX_LINES
    ):
        raise TranscriptionError(
            "Too many lines for one receipt; list each printed line exactly once."
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

    def amount_or_none(amount: float | None) -> int | None:
        # A printed tax of 0 is printed: it says the rate's items carry no tax.
        return None if amount is None else abs(minor(amount))

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
                tax_amount=amount_or_none(line.tax_amount),
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
    """``amount`` is ``percentage`` of these lines.

    Either rounded once on their sum, or line by line with one rounding rule
    (down, up or half up) for every line: never a mix no till would print.
    """
    if _percent_fits(percentage, sum(bases), amount):
        return True
    exact = [percentage * base / 100 for base in bases]
    return amount in {
        sum(floor(value) for value in exact),
        sum(ceil(value) for value in exact),
        sum(floor(value + Fraction(1, 2)) for value in exact),
    }


class _Budget:
    """The search work left in one allocation."""

    def __init__(self) -> None:
        self.steps = _SEARCH_STEPS

    def spend(self, steps: int) -> bool:
        self.steps -= steps
        return self.steps >= 0


def _unique_subset(
    orders: Sequence[int], weights: Mapping[int, int], target: int, budget: _Budget
) -> tuple[int, ...] | None:
    """The one subset of ``orders`` whose weights sum to ``target``, if exactly one.

    Weights must be positive. Gives up (None) when the budget runs out.
    """
    if sum(weights[order] for order in orders) < target:
        return None
    # Number of subsets reaching each sum (capped at 2), and how each sum was
    # first reached: the sum before it and the position of the item added.
    counts: dict[int, int] = {0: 1}
    first: dict[int, tuple[int, int]] = {}
    for position, order in enumerate(orders):
        if not budget.spend(len(counts)):
            return None
        weight = weights[order]
        for subtotal, count in list(counts.items()):
            reached = subtotal + weight
            if reached > target:
                continue
            seen = counts.get(reached, 0)
            if not seen:
                first[reached] = (subtotal, position)
            counts[reached] = min(2, seen + count)
    if counts.get(target) != 1:
        return None
    # With exactly one subset, the first way each sum on its path was reached
    # is that subset.
    chosen: set[int] = set()
    reached = target
    while reached:
        reached, position = first[reached]
        chosen.add(position)
    return tuple(order for position, order in enumerate(orders) if position in chosen)


def _nearest_reachable(weights: Iterable[int], target: int) -> int | None:
    """How far ``target`` is from the nearest sum of some of ``weights``.

    None when ``target`` is too large to search.
    """
    if target > _MAX_GROUP_TARGET:
        return None
    # Bit n of ``reachable`` is set when some of the weights sum to n.
    reachable = 1
    mask = (1 << (2 * target + 1)) - 1
    for weight in weights:
        reachable |= (reachable << weight) & mask
    # The highest sum at or below the target (the empty set always reaches 0),
    # and the lowest at or above it.
    below = target - ((reachable & ((1 << (target + 1)) - 1)).bit_length() - 1)
    above = reachable >> target
    if not above:
        return below
    return min(below, (above & -above).bit_length() - 1)


def _scope_from_base(
    base: int,
    candidates: Sequence[tuple[int, ...]],
    live: tuple[int, ...],
    nets: Mapping[int, int],
    budget: _Budget,
) -> tuple[int, ...] | None:
    """The items whose current costs add up to the printed base amount.

    Current costs, not printed prices: the base is what the line was computed
    on at its place in the receipt, so a base that only matches printed prices
    means an earlier line was transcribed out of order.
    """
    for candidate in candidates:
        if candidate and sum(nets[order] for order in candidate) == base:
            return candidate
    return _unique_subset(live, nets, base, budget)


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
    # With no usable hint, settle the line only when exactly one reading fits:
    # one item, every item, or every item without a discount of its own.
    readings: list[tuple[int, ...]] = []
    if (single := _single_item_fit(percentage, amount, live, nets, prices)) is not None:
        readings.append(single)
    for candidate in (live, unspecific):
        if (
            candidate
            and candidate not in readings
            and _percent_fits_lines(
                percentage, [nets[order] for order in candidate], amount
            )
        ):
            readings.append(candidate)
    return readings[0] if len(readings) == 1 else None


def _resolve_scope(
    adjustment: Adjustment,
    nets: Mapping[int, int],
    prices: Mapping[int, int],
    item_specific: set[int],
    budget: _Budget,
) -> tuple[ScopeStatus, tuple[int, ...]]:
    live = tuple(order for order, net in nets.items() if net > 0)
    hint = tuple(order for order in adjustment.item_orders if nets.get(order, 0) > 0)
    unspecific = tuple(order for order in live if order not in item_specific)

    scope: tuple[int, ...] | None
    status: ScopeStatus = "verified"
    if adjustment.eligible_subtotal is not None:
        base = adjustment.eligible_subtotal
        scope = _scope_from_base(base, (hint, live, unspecific), live, nets, budget)
        if adjustment.percentage is not None and not _percent_fits(
            adjustment.percentage, base, adjustment.amount
        ):
            # The percent is taken on the printed base: if they disagree, one of
            # them was misread, whichever items happen to add up to the base.
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
    rates = {line.rate for line in tax_lines if line.rate is not None}
    if len(rates) == 1:
        return next(iter(rates))
    bases: dict[Fraction, int] = {}
    for line in tax_lines:
        if line.rate is not None and line.taxable_amount is not None:
            bases[line.rate] = bases.get(line.rate, 0) + line.taxable_amount
    if not bases:
        return None
    # Ties go to the higher rate.
    return max(bases, key=lambda rate: (bases[rate], rate))


def _complete_tax_lines(tax_lines: Sequence[TaxLine]) -> list[TaxLine] | None:
    """The rated tax lines, when every one prints its rate, base and tax."""
    rated = [line for line in tax_lines if line.rate is not None]
    if not rated or any(
        line.taxable_amount is None or line.tax_amount is None for line in rated
    ):
        return None
    return rated


def _tax_form(rate: Fraction, base: int, tax: int) -> str | None:
    """Whether ``tax`` sits inside ``base`` or is added on top of it, if only one fits.

    Inside, the tax is ``rate / (100 + rate)`` of the base; on top it is
    ``rate / 100`` of it; either rounded any way.
    """
    inside = abs(base * rate - (100 + rate) * tax) < 100 + rate
    on_top = abs(base * rate - 100 * tax) < 100
    if inside == on_top:
        return None
    return "inside" if inside else "on top"


def _prices_include_tax(
    tax_lines: Sequence[TaxLine],
    complete: Sequence[TaxLine] | None,
    subtotals: Sequence[int],
) -> bool | None:
    """What the printed tax lines say about the item prices, when they say anything.

    ``subtotals`` are the items with and without the points taken off, since
    points paid after tax leave the bases unchanged. With complete lines, a
    base that includes its tax means the prices do; a base before tax means the
    prices include tax when they add up to the bases plus the tax, and exclude
    it when they add up to the bases alone. Without bases, one rate's tax is
    read against the items themselves, and several rates' tax that no price
    could hold inside it means the tax was added on top.
    """
    if complete is not None:
        forms = {
            _tax_form(
                line.rate or Fraction(0), line.taxable_amount or 0, line.tax_amount or 0
            )
            for line in complete
            if line.rate
        } - {None}
        if forms == {"inside"}:
            return True
        if forms == {"on top"}:
            bases = sum(line.taxable_amount or 0 for line in complete)
            tax = sum(line.tax_amount or 0 for line in complete)
            if bases + tax in subtotals:
                return True
            if bases in subtotals:
                return False
        return None
    taxed = [line for line in tax_lines if line.rate and line.tax_amount is not None]
    if not taxed or any(line.taxable_amount is not None for line in taxed):
        return None
    if len(taxed) == 1:
        (line,) = taxed
        # Read against each subtotal; only readings that agree say anything.
        forms = {
            _tax_form(line.rate or Fraction(0), subtotal, line.tax_amount or 0)
            for subtotal in subtotals
        } - {None}
        if len(forms) == 1:
            return forms == {"inside"}
        return None
    # Tax inside the prices at rate r, printed as t, needs a base above
    # (t - 1)(100 + r) / r; the rates' bases are separate parts of the items.
    least = sum(
        ((line.tax_amount or 0) - 1) * (100 + (line.rate or 0)) / (line.rate or 1)
        for line in taxed
    )
    return False if least >= max(subtotals) else None


Reading = Literal["included", "added", "spread", "spread by rate", "before points"]


def _reading(
    tax_lines: Sequence[TaxLine], subtotal: int, due: int, points: int
) -> Reading | None:
    """How the items, at ``subtotal``, come to the amount due, if they do.

    - included: they are the amount due (tax inside the prices, or none);
    - added: one printed rate added on top, which a client adds itself;
    - spread: tax printed with no rate, to be spread into the costs;
    - spread by rate: tax at printed rates, each into the items its base names;
    - before points: the amount due was read before points were taken off.
    """
    complete = _complete_tax_lines(tax_lines)
    includes_tax = _prices_include_tax(
        tax_lines, complete, (subtotal, subtotal + points)
    )
    rates = sorted({line.rate for line in tax_lines if line.rate})
    rated_tax = sum(line.tax_amount or 0 for line in tax_lines if line.rate is not None)
    unrated_tax = sum(line.tax_amount or 0 for line in tax_lines)
    if subtotal == due and includes_tax is not False:
        return "included"
    if (
        len(rates) == 1
        and includes_tax is not True
        and due - subtotal in _added_tax(subtotal, rates[0])
    ):
        printed = [
            line.tax_amount
            for line in tax_lines
            if line.rate == rates[0] and line.tax_amount is not None
        ]
        # A printed tax amount must be what was added, to the unit.
        if not printed or due - subtotal == sum(printed):
            return "added"
    if (
        not any(line.rate is not None for line in tax_lines)
        and unrated_tax
        and subtotal
        and subtotal + unrated_tax == due
    ):
        return "spread"
    if includes_tax is False and complete is not None and subtotal + rated_tax == due:
        return "spread by rate"
    if (
        points
        and includes_tax is not False
        and subtotal == due - points
        and due - points > 0
    ):
        return "before points"
    return None


def _tax_by_items(
    lines: Sequence[TaxLine], nets: Mapping[int, int], tolerance: int, budget: _Budget
) -> list[tuple[int, tuple[int, ...]]] | None:
    """Each printed rate's tax with the items it applies to, found from its base.

    The items of every rate but the largest are the one set whose costs add up
    to that rate's printed base; the largest rate takes the rest, which must add
    up to its base too. A store may round each rate's share of a deduction
    separately, so a base is matched within ``tolerance``, nearest first.
    ``lines`` are the complete tax lines. None when the bases do not single out
    the items.
    """
    lines = sorted(lines, key=lambda line: line.taxable_amount or 0)
    remaining = tuple(order for order, net in nets.items() if net > 0)
    taxed: list[tuple[int, tuple[int, ...]]] = []
    for line in lines[:-1]:
        base = line.taxable_amount or 0
        targets = [base] + [
            target
            for distance in range(1, tolerance + 1)
            for target in (base - distance, base + distance)
        ]
        items = next(
            (
                found
                for target in targets
                if target > 0
                and (found := _unique_subset(remaining, nets, target, budget))
                is not None
            ),
            None,
        )
        if items is None:
            return None
        taxed.append((line.tax_amount or 0, items))
        remaining = tuple(order for order in remaining if order not in items)
    largest = lines[-1]
    if (
        not remaining
        or abs(sum(nets[order] for order in remaining) - (largest.taxable_amount or 0))
        > tolerance
    ):
        return None
    taxed.append((largest.tax_amount or 0, remaining))
    return taxed


def _bases_disagree(
    tax_lines: Sequence[TaxLine], subtotal: int, total: int, points: int
) -> int:
    """How far the printed taxable bases are from this receipt's totals, or 0.

    Uses the rated lines when every one prints its base, otherwise lines that
    print a base without a rate (when no rated line prints one). Tax-inclusive
    bases add up to the amount paid, or to the amount before points when points
    were a payment; tax-exclusive bases add up to the items before tax.
    """
    rated = [line for line in tax_lines if line.rate is not None]
    if rated and all(line.taxable_amount is not None for line in rated):
        lines = rated
    elif not any(line.taxable_amount is not None for line in rated):
        lines = [line for line in tax_lines if line.taxable_amount is not None]
    else:
        return 0
    if not lines:
        return 0
    bases = sum(line.taxable_amount or 0 for line in lines)
    if bases in (total, total + points, subtotal):
        return 0
    if all(line.tax_amount is not None for line in lines):
        tax = sum(line.tax_amount or 0 for line in lines)
        if bases + tax in (total, total + points):
            return 0
    return bases - total


def _rate_groups_gap(
    tax_lines: Sequence[TaxLine],
    nets: Mapping[int, int],
    nets_before_points: Mapping[int, int],
    total: int,
    points: int,
    tolerance: int,
) -> int:
    """How far the costs are from splitting into the printed per-rate bases, or 0.

    Checked when prices include tax and every rate prints its base: some items
    must add up to each smaller rate's base (the largest takes the rest),
    within one unit per deduction spread over several items, since a till may
    round each rate's share of it separately.
    """
    rated = [line for line in tax_lines if line.rate is not None]
    if len({line.rate for line in rated}) < 2 or any(
        line.taxable_amount is None for line in rated
    ):
        return 0
    targets: dict[Fraction, int] = {}
    with_tax: dict[Fraction, int] = {}
    for line in rated:
        rate = line.rate or Fraction(0)
        targets[rate] = targets.get(rate, 0) + (line.taxable_amount or 0)
        with_tax[rate] = (
            with_tax.get(rate, 0) + (line.taxable_amount or 0) + (line.tax_amount or 0)
        )
    costs = nets
    if sum(targets.values()) == total + points and points:
        # Points paid after tax: the bases are the items before them.
        costs = nets_before_points
    elif sum(targets.values()) != total:
        if (
            all(line.tax_amount is not None for line in rated)
            and sum(with_tax.values()) == total
        ):
            targets = with_tax
        else:
            return 0
    for target in sorted(targets.values())[:-1]:
        distance = _nearest_reachable(costs.values(), target)
        if distance is not None and distance > tolerance:
            return distance
    return 0


def _tax_read_as_surcharge(
    tax_lines: Sequence[TaxLine],
    allocated: Sequence[AllocatedAdjustment],
    subtotal: int,
) -> int | None:
    """The surcharge that repeats the printed tax lines, if one does.

    Tax added on top is copied into the tax lines; a surcharge for exactly the
    printed tax, on no particular items, while the bases add up to the items
    without it, is that tax read a second time.
    """
    complete = _complete_tax_lines(tax_lines)
    if complete is None:
        return None
    positive = [line for line in complete if line.rate]
    if not positive or any(
        _tax_form(
            line.rate or Fraction(0), line.taxable_amount or 0, line.tax_amount or 0
        )
        != "on top"
        for line in positive
    ):
        return None
    tax = sum(line.tax_amount or 0 for line in complete)
    bases = sum(line.taxable_amount or 0 for line in complete)
    for index, entry in enumerate(allocated):
        adjustment = entry.adjustment
        if (
            adjustment.kind == "surcharge"
            and adjustment.percentage is None
            and adjustment.eligible_subtotal is None
            and not adjustment.item_orders
            and adjustment.amount == tax
            and bases == subtotal - adjustment.amount
        ):
            return index
    return None


def _unresolve(entry: AllocatedAdjustment) -> AllocatedAdjustment:
    return AllocatedAdjustment(
        entry.adjustment, "unresolved", entry.adjustment.item_orders, {}
    )


def _chosen_status(
    adjustment: Adjustment,
    scope: tuple[int, ...],
    nets: Mapping[int, int],
    prices: Mapping[int, int],
) -> ScopeStatus:
    """A chosen scope is verified, unless the printed base or percent disagrees with it."""
    if (
        adjustment.eligible_subtotal is not None
        and adjustment.eligible_subtotal != sum(nets[order] for order in scope)
    ):
        return "assumed"
    if adjustment.percentage is not None and not any(
        _percent_fits_lines(
            adjustment.percentage, [basis[order] for order in scope], adjustment.amount
        )
        for basis in (prices, nets)
    ):
        return "assumed"
    return "verified"


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
    budget = _Budget()
    prices = {item.item_order: item.price for item in receipt.items}
    nets = dict(prices)
    item_specific: set[int] = set()
    allocated: list[AllocatedAdjustment] = []

    for index, adjustment in enumerate(receipt.adjustments):
        status: ScopeStatus
        if scopes is None:
            status, scope = _resolve_scope(
                adjustment, nets, prices, item_specific, budget
            )
        else:
            scope = _chosen_scope(adjustment, scopes[index], nets)
            status = _chosen_status(adjustment, scope, nets, prices)
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
    return _reconcile(receipt, prices, nets, allocated, budget)


def _reconcile(
    receipt: PrintedReceipt,
    prices: Mapping[int, int],
    nets: dict[int, int],
    allocated: list[AllocatedAdjustment],
    budget: _Budget,
) -> Allocation:
    due = receipt.amount_due
    display_rate = _display_rate(receipt.tax_lines)

    def outcome(
        status: ReceiptStatus,
        entries: Sequence[AllocatedAdjustment] = allocated,
        difference: int = 0,
    ) -> Allocation:
        # Nothing is allocated unless everything reconciles. Every cost is then
        # 0, which a client that checks the costs against the total always
        # rejects: printed prices could add up to a misread total.
        return Allocation(
            status=status,
            costs=dict.fromkeys(prices, 0),
            total=due,
            tax_rate=display_rate,
            tax_added=False,
            difference=difference,
            adjustments=tuple(entries),
        )

    points = sum(
        adjustment.amount
        for adjustment in receipt.adjustments
        if adjustment.kind == "points"
    )
    if any(entry.status == "unresolved" for entry in allocated):
        # Whichever items the unsettled lines cover, the items come to the same
        # subtotal. If even that cannot add up, the receipt was misread.
        subtotal = sum(prices.values()) + sum(
            adjustment.amount if adjustment.kind == "surcharge" else -adjustment.amount
            for adjustment in receipt.adjustments
        )
        if _reading(receipt.tax_lines, subtotal, due, points) is None:
            return outcome("mismatch", difference=due - subtotal)
        return outcome("needs_review")

    subtotal = sum(nets.values())
    if (
        repeated := _tax_read_as_surcharge(receipt.tax_lines, allocated, subtotal)
    ) is not None:
        entries = list(allocated)
        entries[repeated] = _unresolve(entries[repeated])
        return outcome("needs_review", entries)

    reading = _reading(receipt.tax_lines, subtotal, due, points)
    total, tax_added = due, False
    if reading is None:
        # The amount due less the items, or, when they agree but the printed
        # tax lines say the prices exclude tax, the tax that is missing.
        rated_tax = sum(
            line.tax_amount or 0 for line in receipt.tax_lines if line.rate is not None
        )
        return outcome("mismatch", difference=due - subtotal or -rated_tax)
    if reading == "added":
        # One printed rate added on top: the costs stay before tax and the
        # client adds the rate, exactly as it does for such receipts today.
        tax_added = True
        display_rate = sorted({line.rate for line in receipt.tax_lines if line.rate})[0]
    elif reading == "spread":
        # Tax added on top with no printed rate: no rate for the client to add,
        # so the printed tax goes into the costs in proportion.
        tax = sum(line.tax_amount or 0 for line in receipt.tax_lines)
        for order, share in split_proportionally(tax, nets).items():
            nets[order] += share
    elif reading == "spread by rate":
        # Tax added on top at printed rates the client cannot add itself: each
        # rate's tax goes into the items its printed base singles out.
        complete = _complete_tax_lines(receipt.tax_lines) or []
        spanning = sum(1 for entry in allocated if len(entry.allocations) > 1)
        taxed = _tax_by_items(complete, nets, spanning, budget)
        if taxed is None:
            return outcome("mismatch", difference=due - subtotal)
        for tax, items in taxed:
            shares = split_proportionally(tax, {order: nets[order] for order in items})
            for order, share in shares.items():
                nets[order] += share
    elif reading == "before points":
        # The model copied the total printed before the points were taken off.
        total = due - points

    if difference := _bases_disagree(receipt.tax_lines, subtotal, total, points):
        return outcome("mismatch", difference=difference)
    if reading in ("included", "before points"):
        nets_before_points = dict(nets)
        for entry in allocated:
            if entry.adjustment.kind == "points":
                for order, share in entry.allocations.items():
                    nets_before_points[order] += share
        spanning = sum(1 for entry in allocated if len(entry.allocations) > 1)
        if gap := _rate_groups_gap(
            receipt.tax_lines, nets, nets_before_points, total, points, spanning
        ):
            # No split of the costs matches the printed per-rate bases. Lines
            # placed on the model's word go back to a person; otherwise the
            # receipt was misread.
            if any(entry.status == "assumed" for entry in allocated):
                return outcome(
                    "needs_review",
                    [
                        _unresolve(entry) if entry.status == "assumed" else entry
                        for entry in allocated
                    ],
                )
            return outcome("mismatch", difference=gap)
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
    if (
        len(items) > MAX_ITEMS
        or len(adjustments) > MAX_ADJUSTMENTS
        or len(tax_lines) > MAX_TAX_LINES
    ):
        raise ValueError("Too many lines for one receipt.")
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

    def amount_or_none(amount: float | None) -> int | None:
        return None if amount is None else abs(to_minor_units(amount, places))

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
                tax_amount=amount_or_none(tax),
            )
            for rate, base, tax in tax_lines
        ),
        amount_due=to_minor_units(amount_due, places),
        decimal_places=places,
    )
