# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the human-facing reference: full setup, the env-var table, the API reference with example payloads, the CI overview, and the architecture diagram.
**This file is the agent's operating guide:** the non-obvious, gotcha-level context to read before changing anything financial, infra, or test-related.
Don't duplicate the README; consult it for the full picture.

## What this is

A Django + Django-Ninja REST API that turns a photo receipt into a settled [Settle Up](https://settleup.io) group expense.
It is a **stateless middleman**, not a typical Django app: each app's `models.py` is empty and there is no `admin.py`.
There is no app-owned data; state lives in Firebase (via Settle Up), Redis (cache), Cloudinary (images), or the client.
The SQLite DB exists only for Django's built-in apps and is untouched by the tests.

## Commands

Everything runs through **uv** on Python 3.14, pinned in `.python-version` (uv installs it if missing).
Python 3.15 needs Django 6.1, so the project stays on 3.14 while it is on the Django 5.2 LTS line.
`uv run` re-locks and syncs automatically when `pyproject.toml` changes.

```bash
uv sync                                   # install ALL deps incl. the dev group (uv includes dev by default)
uv sync --no-dev                          # runtime deps only (prod parity)

uv run python manage.py runserver         # local dev server (async views work under runserver)
uv run hypercorn settledown.asgi:application --bind 0.0.0.0:8000   # how prod/Docker runs it

uv run pytest                             # tests (no Redis/network needed; all boundaries mocked; `live` deselected)
uv run pytest --cov                       # with the 100% line+branch coverage gate CI enforces
uv run pytest settle_up/tests/test_transaction.py::TestTransaction::test_fractional_tax_balances_to_total
uv run pytest -m live                     # opt-in: real OpenRouter call (needs a valid key + receipt image)

uv run black .                            # format (CI: --check)
uv run ruff check .                       # lint, incl. bandit (S), Django (DJ), async (ASYNC) rules
uv run mypy .                             # types (django-stubs + pydantic plugins)
uv run lint-imports                       # architecture contracts
uv run deptry .                           # declared deps == imported deps

UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest settledown/tests/test_openapi_schema.py   # after an intentional API change
```

`uv run python manage.py migrate` is **optional**: there are no app models or migrations, only Django's built-ins.
The running app **does** require Redis (`django_redis` cache backend) and the env vars documented in `README.md`; the tests do not.

## Architecture

Request entry is `settledown/api.py`: one `NinjaAPI` protected by `GlobalAuth`, an `HttpBearer` that compares the token to `settings.APP_AUTH` in constant time and rejects everything when `APP_AUTH` is unset.
The layout follows the Django Ninja docs: the project package `settledown/` only wires the apps together, importing each app's router and mounting it (`api.add_router("/v1/receipts/", receipts_router)`).
Each app (`receipts/`, `settle_up/`) keeps its routes on a tagged `Router` in `api.py`, its schemas in `schemas.py`, and its services in `services.py`.
Routes call the functions below them directly, map their errors to HTTP and build their response; the money math and the upstream calls never live in a route, and the two apps never import each other or the project package.

- `/api/v1/receipts/receipt-items/` (`receipts/api.py`) is **the OCR flow**: the route refuses a photo over `RECEIPT_IMAGE_MAX_BYTES` with a 413 before touching it, reads the photo with `ocr.read_receipt()`, hosts it with `image_hosting.upload_receipt_image()`, allocates it with `allocation.allocate_scan()` and maps the result onto `OCRReceiptPostOut` with the response helpers in `services.py` (`item_costs`, `adjustments_out`, `reconciliation_out`), which the allocations route shares.
  `ocr.read_receipt()` runs an async `pydantic-ai` `Agent` from `get_openrouter_receipt_agent()` (OpenRouter, model `settings.OPENROUTER_MODEL`), which reads the uploaded image with a strict `ToolOutput(ReceiptScan)` (`receipts/transcription.py`).
  The image's media type comes from its bytes (`ocr.image_media_type`), because uploads are phone photos whose declared type is often generic.
  The model only **transcribes**: printed item prices, every printed line that changes the amount to pay (discounts, coupons, points, surcharges, each with its printed amount, percent, base and the items it names), the tax lines and the amount due.
  It never nets a discount into a price or computes a total.
  `receipts/allocation.py` does all of that money math (see below), and the response's `cost` is each line's share after every adjustment.
  It has one tool, `translate_jp_to_en_text` (Google Translate).
  The output validator (`validate_receipt_scan`) asks for a re-read only when a transcription cannot be read as money, and for at most one targeted re-read when it does not add up; it records every attempt in `ScanAttempts` (the run's deps) and the run ends with the best one, not the last.
  If a later read fails outright, `read_receipt()` still answers with the best earlier attempt.
  `get_receipt_agent()` is an alternate OpenAI/LLM7 factory (`gpt-5-mini`) wrapped by `receipts/llm7.py` (`LLM7ChatModel`), a parsing workaround; don't delete it as dead code.
  The image is then hosted by `image_hosting.upload_receipt_image()` on Cloudinary, falling back to catbox.moe on any exception (logged with its traceback).
  The SDK and `requests` are blocking, so the route awaits `sync_to_async(upload_receipt_image)`: Django's ASGI handler gives each request its own worker thread, and the upload runs there, off the event loop.
  It stays after the read on purpose: a failed scan uploads nothing, and an upload already running in a thread cannot be cancelled (catbox uploads are public and cannot be deleted).
- `/api/v1/receipts/allocations/` recalculates a scanned receipt with the items a person chose for each adjustment (the app's review step), through `services.recalculate_receipt()`; no model call.
  A choice that cannot be applied raises `ReceiptCorrectionError`, which the route turns into a 422.
  Only the allocation is guarded, so a fault while building the response (a pydantic `ValidationError` is a `ValueError`) stays a 500.
- `/api/v1/settle-up/...` (`settle_up/api.py`) lists groups, lists members, and creates a transaction.
  All delegate to `SettleUpClient` (`settle_up/services.py`).
  The app deletes its draft on any 2xx from the transaction route, so a write Settle Up refuses (a 4xx, `SettleUpWriteError.refused`) is a 424 and one it fails (a 5xx) is a 502, never a 204; a body with nothing to split (`NothingToSplitError`) is a 422.

Swagger UI is at `/api/docs` (no trailing slash; authorize with the `APP_AUTH` bearer token).

### `SettleUpClient` is the core (`settle_up/services.py`)

Constructed fresh per request.
`__init__` signs in through the Firebase Auth REST API (`settle_up/firebase_auth.py`) and caches the credentials in Redis (~3500s, about 58 min).
`get_groups` / `get_group` / `get_group_members_by_group` hit the Settle Up REST API and cache results for ~24h (`timeout=86500`), **so changes made in Settle Up won't appear until that cache expires.**
Every upstream HTTP call passes `timeout=settings.UPSTREAM_HTTP_TIMEOUT`; ruff's `S113` fails CI if a new one doesn't.
A missing group (Firebase answers `null`) raises `SettleUpGroupNotFoundError`.

### Receipt allocation (`receipts/allocation.py`)

Pure, integer minor units, and the only place receipt discounts are computed.
The normative rules are in its module docstring and pinned by `receipts/tests/fixtures/allocation-vectors.json`; read both before changing anything.

- **Scope** of each adjustment is resolved from printed numbers: a printed base (`対象金額`) must equal what its items cost at that point in the receipt, and a printed percent must fit its items within one unit.
  The items the model names are only a hint, which is what recovers the misreads seen live (a discount attached to the line above it).
  No printed evidence means the named items or the whole receipt, marked `assumed`; evidence that fits nothing or several things is `unresolved`.
- **Allocation** is in printed order, by largest remainder with exact integer remainders, ties to the lower `item_order`.
  Don't reuse the app's `allocateMoney` logic: it ranks float remainders and breaks exact ties differently.
- **Reconciliation**: the costs must equal the amount due, or do with one printed tax rate added on top (then `cost` stays pre-tax and `tax_percentage` carries the rate, today's shape), or with the points taken off a total the model read before them; printed per-rate taxable bases must agree.
  Printed tax the app cannot add itself (no rate printed, or several rates whose printed bases single out their items) is spread into those items' costs.
  Printed tax lines that say the prices include or exclude tax rule out the readings that contradict them, including a rate and tax printed without a base, read against the items.
  When prices include tax and every rate prints its base, some items must make up each base (one unit of slack per line spread over several items); if none do, lines placed on the model's word go back to review.
  A surcharge that repeats the printed tax total is tax read twice and goes to review.
- **Bounded work**: exact searches over item sets share one step budget per allocation (`_SEARCH_STEPS`) and give up by failing closed; the per-rate base check builds one bitset per allocation, at most `2 * _MAX_GROUP_TARGET` bits wide, and skips any item larger than it; amounts and line counts are capped (`MAX_AMOUNT`, `MAX_ITEMS`, ...), and the corrections schema caps its lists, so a crafted body cannot tie up a worker.
- **Fail closed**: `needs_review` or `mismatch` allocates nothing and every `cost` is 0, which the installed app's check always rejects (it refuses a subtotal of 0).
  Don't return printed prices there: they can add up to a misread total (the subtotal before a coupon) and would be filed as is.
  `receipts/tests/client_gate.py` restates that app check, and the vectors assert it accepts exactly the reconciled results.
- Redeemed points always reduce what the group splits, whether printed as a discount or as a payment.

### Transaction money (`settle_up/splitting.py`)

The transaction money logic lives in `settle_up/splitting.py` (pure functions, wrapped by `SettleUpClient._compute_transaction` / `_compute_weights`).
Understand both before touching anything financial:

- **`compute_member_totals()`** turns per-member items + shared items + a tax % + the trusted `total_amount` into `{member_id: amount_owed}` (in the group's currency).
  It does not know whether the receipt's printed total already includes consumption tax, so it *infers* it: `should_compute_tax` is an **exact float `==` comparison** of `(pre-tax items + computed tax + shared) == total_amount`.
  If equal, tax was excluded and gets added to everyone; if not, tax is assumed already baked in and is not added.
  This is held together by `round(_, 2)` on every tax term; those rounds are load-bearing, not cosmetic, and `test_every_tax_term_rounds_to_cents` pins them.
- **`compute_weights()`** reduces the per-member totals to the smallest integer ratio (`round(s*100)` then divide by the GCD).
  In `create_transaction`, those GCD-reduced weights go into the transaction's `forWhom`, while `whoPaid` carries the **full `total_amount`** as the payer's weight; don't conflate the two.
  The currency comes from the group document (`convertedToCurrency`, strict access) with an identity `exchangeRates` map.

### Constraints baked into the current model (don't assume otherwise)

- On the transaction endpoint, tax is a single scalar applied **all-or-nothing**.
  Scans with mixed rates work when tax is inside the prices (the costs already carry it); tax added on top at several rates, or a deduction after tax, does not reconcile and goes to review.
- The transaction endpoint trusts `total_amount` and never checks it; the scan endpoint reconciles every scan against the printed totals before returning it.
- The float-`==` tax heuristic means a one-unit rounding drift can silently flip the entire tax decision.
  Be careful changing any rounding or the comparison.
- Transactions are filed in the group's currency, but there is **no FX conversion**: a receipt denominated differently from its group is recorded unconverted.
  Because the group document is cached ~24h, a currency changed in Settle Up stays stale here for up to a day, and that staleness affects money, not just display.

### Architecture contracts (`[tool.importlinter]` in `pyproject.toml`)

CI does not run these; check them locally with `uv run lint-imports` before changing imports between modules.

- The apps never import `settledown`, and `receipts` and `settle_up` never import each other.
- Inside each app, a module may only import the layers below it, and siblings separated by `|` may not import each other:
  - `receipts`: `api` > `services` > `ocr | image_hosting | schemas` > `allocation | llm7` > `transcription`.
  - `settle_up`: `api` > `services` > `schemas` > `splitting | firebase_auth` > `dto`.
- `receipts.allocation` and `settle_up.splitting` must not import Django, ninja, `requests`, or any SDK, which is why `splitting` types its inputs structurally (`MemberCost` protocol) instead of importing `schemas.UserTransactionSchema`, and why `receipts/transcription.py` must stay Django-free.

A new module is unchecked until it is listed in its app's layers; a new pure module also belongs in the purity contract's `source_modules` and the strict mypy override.

## Gotchas

- **Every warning is an error in tests** (`filterwarnings = ["error"]`).
  A dependency upgrade that deprecates something we call fails the suite; fix the call (as with ninja's `Status(204, None)`), don't blanket-ignore.
- **Coverage must stay at 100%** (lines and branches, `fail_under = 100`), so new code ships with tests.
- **`openapi.json` is a committed snapshot.** Any change to routes or schemas fails `test_openapi_schema.py` until you regenerate it, so every contract change shows up in the diff.
- **Pydantic `Field` defaults must be keywords** (`Field(default=None, ...)`): mypy reads ninja schemas through `dataclass_transform`, which ignores a positional default and then reports the field as required.
- **Upgrade pydantic-ai and openai together.** `pydantic-ai-slim<2` targets the openai 2.x SDK, so `openai` is capped `<3`.
- **`LLM7ChatModel._process_response` is marked `@override`**, so mypy fails if pydantic-ai renames the hook it patches.
- Live OCR tests read `OPENROUTER_API_KEY` from `.env`; they are deselected by default, so a stale local key no longer breaks `uv run pytest`.

## Testing

Tests live in each app's `tests/` package, and the project wiring's in `settledown/tests/` (bearer auth, the OpenAPI snapshot, the server entrypoints).
`settle_up/tests/conftest.py` provides the `settle_up_client` fixture: a real `SettleUpClient` with `firebase_auth`, `requests`, and `cache` patched, so no login/network/Redis happens.
The fixture routes the mocked `requests.get` **by URL**: `groups/{id}.json` resolves to a mutable `group_json` (default `GROUP_INFO`, currency JPY) that tests change in place to simulate another currency; every other URL falls through to the shared `return_value`.
The mocked `requests.post` answers with a real `requests.Response` (`firebase_answer(200, {"name": "tx-1"})`), because a `MagicMock` response is always `ok`; build error answers with `firebase_answer` too.
The mocked members endpoint returns exactly **Member 1 and Member 2** (`GROUP_MEMBERS`), and the split-related assertions in `test_transaction.py` depend on that two-member group.

- `test_transaction.py` exercises `_compute_transaction()` across tax-included / tax-excluded / shared-split / fractional-tax / cent-rounding scenarios.
- `receipts/tests/test_allocation.py` runs every golden vector through conversion, scope resolution, allocation, reconciliation and the app-check restatement; `receipts/tests/scans.py` builds model transcriptions from the same vectors for the other tests.
  Expected results in the vectors are worked out by hand, never pasted from the code's output.
- `test_ocr_validator.py` drives the real agent loop offline with a scripted `FunctionModel` and pins the number of model requests per scan.
- `test_ocr_agent.py::TestWireSchema` captures what each provider path sends the model (via `httpx.MockTransport`) and asserts every transcribed field is required with no default; on the Gemini path a default would make it silently optional.
- Each app's `tests/test_api.py` drives its routes through Django's test `Client` (URL conf, middleware, auth) with the outbound boundaries mocked.
  A test that patches `cloudinary_upload_file` must patch `catbox_upload_file` too: the fallback catches every exception, including a failed assertion inside the Cloudinary mock, and would otherwise make a real catbox upload.

pytest is configured in `pyproject.toml` (`DJANGO_SETTINGS_MODULE=settledown.settings`, `testpaths`, `-m "not live"`, strict markers/config).

## CI

`.github/workflows/` has three workflows (details in README → Continuous integration):

- `ci.yml` (PRs and `main`): lint (ruff, actionlint), mypy, pytest with the coverage gate, Django `check` + `makemigrations --check`.
  The `CI passed` job aggregates them for branch protection.
- `security.yml` (PRs, `main`, weekly): pip-audit.
- `nightly.yml`: latest-dependencies canary (`uv lock --upgrade`, then the tests and mypy).
  Live OCR tests (`-m live`) run only locally.

All actions are pinned to exact version tags (e.g. `astral-sh/setup-uv@v10.3.3`), not commit SHAs.
Container images are pinned to digests.
Keep the lint/format commands here, in CI, in `.pre-commit-config.yaml`, in the README, and in the `.claude/settings.json` edit hook in sync.
That hook runs `ruff check --fix` then `black` on every edited `.py` file; `bash scripts/verify-format-hook.sh` checks it still works.
