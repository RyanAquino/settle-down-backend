# Group-Currency Transactions — Design

**Date:** 2026-08-10
**Status:** Approved (interactive design review, this session)
**Reference:** [Settle Up Public API](https://api.settleup.io/) — [Data entities](https://api.settleup.io/entities/)

## Problem

`SettleUpClient.create_transaction` hardcodes `"currencyCode": "JPY"` and
`"exchangeRates": {"JPY": "1"}` (`backend_api/settleup_utils.py`). Every
expense is filed as yen regardless of the group it lands in, so the app only
works correctly for Japanese groups. TWD/HKD receipts posted to a Taiwan or
Hong Kong group are recorded as yen-denominated numbers.

## Facts from the Settle Up API (verified against api.settleup.io/entities/)

- A **group** document (`GET /groups/{groupId}.json`) carries its currency as
  `convertedToCurrency` (e.g. `"USD"`). There is no `defaultCurrency` field.
- A **transaction** carries `currencyCode` (the currency the expense was paid
  in) and an `exchangeRates` map used to convert amounts into other
  currencies, including the group's.
- The API exposes **no exchange-rates or currencies endpoint**. Rates on a
  transaction are supplied by the client that creates it.

Consequence: for a transaction filed *in the group's own currency*, the
identity map `{<currency>: "1"}` is correct and is the only implementable
option without an external FX provider. This mirrors the currently-proven
JPY-in-JPY-group behavior.

## Decision

**Approach A — file every transaction in its group's currency.**

Receipt amounts are assumed to be in the group's currency (a Taiwan receipt is
filed into a TWD group). Cross-currency conversion (receipt currency ≠ group
currency) stays out of scope and remains the documented follow-up in
CLAUDE.md.

Rejected alternatives:

- **B. Receipt currency + real FX:** thread OCR-detected currency through
  `TransactionPostIn` and convert with real rates. Needs an external FX
  provider (Settle Up has none), new schema fields, and client changes.
- **C. Client-supplied `currency_code` override:** YAGNI; trivial to add later.

## Design

### `SettleUpClient` (`backend_api/settleup_utils.py`)

- **New `get_group(group_id) -> dict`** — fetches
  `GET /groups/{group_id}.json`, cached ~24h (`timeout=86500`) under
  `{group_id}_settle_up_group`. Same shape/caching pattern as
  `get_group_members_by_group`. Returns the raw group document.
- **`get_groups()` reuses `get_group()`** for the per-group fetch inside its
  loop. Same HTTP call count; each group document becomes individually cached,
  so a later `create_transaction` gets a warm cache hit. Populates the new
  currency field on the DTO via `group.get("convertedToCurrency")` (tolerant:
  one malformed group must not break the whole listing).
- **`create_transaction()`** resolves
  `currency = self.get_group(payload.group_id)["convertedToCurrency"]`
  (strict key access — if the field is ever missing, fail loudly rather than
  silently file money under a guessed currency) and builds:
  - `"currencyCode": currency`
  - `"exchangeRates": {currency: "1"}`
  - `fixedExchangeRate`, weights, `whoPaid`, amount formatting: unchanged
    (`compute_weights`/`format_amount`/`split_amount_evenly` are already
    2-decimal-safe for TWD/HKD).

### API surface

- `SettleUpGroup` DTO (`backend_api/dto/settleup.py`) and
  `SettleUpGroupSchema` (`backend_api/schemas.py`) gain
  `currency: str | None = None`.
- `/api/v1/settle-up/groups/` responses now include `currency` — additive,
  backward-compatible for the client app.
- `TransactionPostIn` is unchanged.

### Error handling

- Missing `convertedToCurrency` at transaction time → `KeyError` → 500. Loud
  by design; broader upstream-error surfacing stays deferred to the
  production-hardening plan (P2 item 5).
- Listing path tolerates a missing field (`currency=None`).

### Testing (`backend_api/tests/`)

- **conftest:** route the `requests.get` mock by URL via `side_effect`:
  `/groups/{id}` returns a group document — the yielded namespace exposes it
  as a mutable `group_json` dict (default
  `{"name": "Group A", "convertedToCurrency": "JPY"}`) that tests update
  in place to simulate other currencies — while every other URL falls through to the
  existing `mock_requests.get.return_value`. The established
  `return_value.json.return_value = ...` members-retargeting idiom
  (four-member suite, CLAUDE.md) keeps working unchanged.
- **Existing payload-pinning tests** stay green (default group currency JPY).
- **New tests:**
  - `create_transaction` against a `convertedToCurrency: "TWD"` group →
    body has `currencyCode == "TWD"` and `exchangeRates == {"TWD": "1"}`.
  - `get_groups` returns each group's `currency`.

### Documentation

- CLAUDE.md: rewrite the "files every expense as JPY" constraint bullet —
  currency now follows the group; the remaining limitation is that receipt
  amounts are assumed to be in the group's currency (no FX conversion).
- README: update the `/groups/` example response and any JPY-behavior mention
  in the API reference.

## Success criteria

- Posting a transaction to a TWD/HKD group files it under that currency in
  Settle Up with the identity exchange rate.
- Group listing exposes each group's currency to the client.
- `uv run pytest && uv run ruff check . && uv run black --check .` pass.
