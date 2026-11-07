# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the human-facing reference: full setup, the env-var table, the API reference with example payloads, the CI overview, and the architecture diagram.
**This file is the agent's operating guide:** the non-obvious, gotcha-level context to read before changing anything financial, infra, or test-related.
Don't duplicate the README; consult it for the full picture.

## What this is

A Django + Django-Ninja REST API that turns a photo receipt into a settled [Settle Up](https://settleup.io) group expense.
It is a **stateless middleman**, not a typical Django app: `backend_api/models.py` is empty and there is no `admin.py`.
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
uv run pytest backend_api/tests/test_transaction.py::TestTransaction::test_fractional_tax_balances_to_total
uv run pytest -m live                     # opt-in: real OpenRouter call (needs a valid key + receipt image)

uv run black .                            # format (CI: --check)
uv run ruff check .                       # lint, incl. bandit (S), Django (DJ), async (ASYNC) rules
uv run mypy .                             # types (django-stubs + pydantic plugins)
uv run lint-imports                       # architecture contracts
uv run deptry .                           # declared deps == imported deps

UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest backend_api/tests/test_openapi_schema.py   # after an intentional API change
```

`uv run python manage.py migrate` is **optional**: there are no app models or migrations, only Django's built-ins.
The running app **does** require Redis (`django_redis` cache backend) and the env vars documented in `README.md`; the tests do not.

## Architecture

Request entry is `settledown/api.py`: one `NinjaAPI` protected by `GlobalAuth`, an `HttpBearer` that compares the token to `settings.APP_AUTH` in constant time and rejects everything when `APP_AUTH` is unset.
It mounts two routers under `/api/`:

- `/api/v1/receipts/receipt-items/` (`backend_api/api.py`) is **the OCR flow**.
  An async `pydantic-ai` `Agent` from `get_openrouter_receipt_agent()` (OpenRouter, model `settings.OPENROUTER_MODEL`) reads the uploaded image with a strict `ToolOutput(ReceiptData)`, so the LLM must return a validated `ReceiptData` (items, EN/JP shop names, tax %, total, date) rather than free text.
  It has one tool, `translate_jp_to_en_text` (Google Translate).
  The image is then uploaded to Cloudinary, falling back to catbox.moe on any exception (logged with its traceback).
  `get_receipt_agent()` is an alternate OpenAI/LLM7 factory (`gpt-5-mini`) wrapped by `backend_api/dto/llm7_override.py` (`LLM7ChatModel`), a parsing workaround; don't delete it as dead code.
- `/api/v1/settle-up/...` (`backend_api/settleup_api.py`) lists groups, lists members, and creates a transaction.
  All delegate to `SettleUpClient`.

Swagger UI is at `/api/docs` (no trailing slash; authorize with the `APP_AUTH` bearer token).

### `SettleUpClient` is the core (`backend_api/settleup_utils.py`)

Constructed fresh per request.
`__init__` signs in through the Firebase Auth REST API (`backend_api/firebase_auth.py`) and caches the credentials in Redis (~3500s, about 58 min).
`get_groups` / `get_group` / `get_group_members_by_group` hit the Settle Up REST API and cache results for ~24h (`timeout=86500`), **so changes made in Settle Up won't appear until that cache expires.**
Every upstream HTTP call passes `timeout=settings.UPSTREAM_HTTP_TIMEOUT`; ruff's `S113` fails CI if a new one doesn't.
A missing group (Firebase answers `null`) raises `SettleUpGroupNotFoundError`.

The money logic lives in `backend_api/utils.py` (pure functions, wrapped by `SettleUpClient._compute_transaction` / `_compute_weights`).
Understand both before touching anything financial:

- **`compute_member_totals()`** turns per-member items + shared items + a tax % + the trusted `total_amount` into `{member_id: amount_owed}` (in the group's currency).
  It does not know whether the receipt's printed total already includes consumption tax, so it *infers* it: `should_compute_tax` is an **exact float `==` comparison** of `(pre-tax items + computed tax + shared) == total_amount`.
  If equal, tax was excluded and gets added to everyone; if not, tax is assumed already baked in and is not added.
  This is held together by `round(_, 2)` on every tax term; those rounds are load-bearing, not cosmetic, and `test_every_tax_term_rounds_to_cents` pins them.
- **`compute_weights()`** reduces the per-member totals to the smallest integer ratio (`round(s*100)` then divide by the GCD).
  In `create_transaction`, those GCD-reduced weights go into the transaction's `forWhom`, while `whoPaid` carries the **full `total_amount`** as the payer's weight; don't conflate the two.
  The currency comes from the group document (`convertedToCurrency`, strict access) with an identity `exchangeRates` map.

### Constraints baked into the current model (don't assume otherwise)

- Tax is a single scalar applied **all-or-nothing**; mixed rates (e.g. JP 8% food vs 10%) are not representable.
- `total_amount` is **trusted input**, never validated; the OCR prompt merely asks the LLM to make items sum to it.
- The float-`==` tax heuristic means a one-unit rounding drift can silently flip the entire tax decision.
  Be careful changing any rounding or the comparison.
- Transactions are filed in the group's currency, but there is **no FX conversion**: a receipt denominated differently from its group is recorded unconverted.
  Because the group document is cached ~24h, a currency changed in Settle Up stays stale here for up to a day, and that staleness affects money, not just display.

### Architecture contracts (`[tool.importlinter]` in `pyproject.toml`)

CI does not run these; check them locally with `uv run lint-imports` before changing imports between modules.


Layers, top to bottom: `settledown.urls` > `settledown.api` > routers (`backend_api.api`, `settleup_api`) > integrations (`settleup_utils`, `ocr`, `services`) > `schemas` > `utils`, `firebase_auth` > `dto`.
A layer may only import layers below it, and siblings may not import each other.
`backend_api.utils` must not import Django, ninja, `requests`, or any SDK, which is why it types its inputs structurally (`MemberCost` protocol) instead of importing `schemas.UserTransactionSchema`.

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

`backend_api/tests/conftest.py` provides the `settle_up_client` fixture: a real `SettleUpClient` with `firebase_auth`, `requests`, and `cache` patched, so no login/network/Redis happens.
The fixture routes the mocked `requests.get` **by URL**: `groups/{id}.json` resolves to a mutable `group_json` (default `GROUP_INFO`, currency JPY) that tests change in place to simulate another currency; every other URL falls through to the shared `return_value`.
The mocked members endpoint returns exactly **Member 1 and Member 2** (`GROUP_MEMBERS`), and the split-related assertions in `test_transaction.py` depend on that two-member group.

- `test_transaction.py` exercises `_compute_transaction()` across tax-included / tax-excluded / shared-split / fractional-tax / cent-rounding scenarios.
- `test_http_api.py` drives the API through Django's test `Client` (URL conf, middleware, auth) with the outbound boundaries mocked.

pytest is configured in `pyproject.toml` (`DJANGO_SETTINGS_MODULE=settledown.settings`, `testpaths`, `-m "not live"`, strict markers/config).

## CI

`.github/workflows/` has three workflows (details in README → Continuous integration):

- `ci.yml` (PRs and `main`): lint (ruff, actionlint), mypy, pytest with the coverage gate, Django `check` + `makemigrations --check`.
  The `CI passed` job aggregates them for branch protection.
- `security.yml` (PRs, `main`, weekly): pip-audit.
- `nightly.yml`: latest-dependencies canary (`uv lock --upgrade`, then the tests and mypy).
  Live OCR tests (`-m live`) run only locally.

All actions are pinned to exact version tags (e.g. `astral-sh/setup-uv@v10.3.3`), not commit SHAs.
Container images are pinned to digests, and Dependabot keeps all of them current.
Keep the lint/format commands here, in CI, in `.pre-commit-config.yaml`, and in the README in sync.
