# settledown

> Receipt-splitting backend that extracts expenses from photo receipts via OCR and files them into Settle Up groups with tax-aware cost apportionment.

settledown is a Django REST API that turns a photo of a receipt into a settled group expense. It processes receipt images, extracts itemized costs via multimodal LLM analysis (with Japanese-to-English translation), and automatically creates transaction records in [Settle Up](https://settleup.io/). It handles group-based expense splitting with support for shared items, per-person itemization, and all-or-nothing tax computation, caching Settle Up group and member data in Redis to minimize Firebase API calls.

## License

This project does not currently include a LICENSE file. As such, it is **UNLICENSED** — no usage, distribution, or modification rights are granted by default. Add a standard license (MIT, Apache 2.0, etc.) if you intend to make it reusable.

## Features

- OCR receipt image processing with bilingual (Japanese/English) text extraction
- AI-powered line-item transcription; per-item, subset, coupon and points discounts are allocated to items in pure code and reconciled against the printed totals
- Two-tier expense allocation: per-member itemization plus shared item splitting
- Tax computation with floating-point equality verification against the declared total
- Receipt image hosting via Cloudinary with a catbox.moe fallback
- 24-hour Redis cache of Settle Up groups and group members
- Firebase authentication for the Settle Up service account
- Transaction weight computation via GCD-based rational simplification

## Architecture

settledown exposes a [Django Ninja](https://django-ninja.dev/) API under the `/api` prefix, composed of two routers: a receipts router (OCR extraction) and a settle-up router (groups, users, and transactions). The API delegates to external services for AI, translation, image hosting, group/member metadata, and caching. There is no application database — all state lives in Firebase (Settle Up), Redis, or the client.

```
                         +-----------------------------+
                         |          Client             |
                         +--------------+--------------+
                                        | HTTP (Bearer auth)
                                        v
                    +-------------------------------------------+
                    |      settledown API  (/api, Ninja)        |
                    |                                           |
                    |  /v1/receipts/      (receipts router)     |
                    |  /v1/settle-up/     (settle-up router)    |
                    +----+--------+---------+---------+----+-----+
                         |        |         |         |    |
          +--------------+        |         |         |    +---------------+
          v                       v         v         v                    v
   +-------------+      +-----------------+  |   +-----------+      +--------------+
   | OpenRouter  |      | Google Translate|  |   |  Redis    |      | Firebase /   |
   | (Gemini)    |      | (googletrans)   |  |   |  cache    |      | Settle Up    |
   | Pydantic AI |      +-----------------+  |   +-----------+      | (REST)       |
   +-------------+                           |                      +--------------+
                                             v
                                 +-----------------------+
                                 | Cloudinary (primary)  |
                                 | catbox.moe (fallback) |
                                 +-----------------------+
```

### Flow 1 — OCR Receipt Processing

1. The client uploads a receipt image to `POST /api/v1/receipts/receipt-items/`.
2. The endpoint initializes a Pydantic AI agent backed by the OpenRouter provider (default `google/gemini-3-flash-preview`, via `get_openrouter_receipt_agent`). An alternate OpenAI/LLM7 factory (`get_receipt_agent`) remains available.
3. The agent receives a system prompt plus the image, and **transcribes** the receipt: printed item prices, every printed line that changes the amount to pay (discounts, coupons, points, surcharges, each with its printed amount, percent, base amount and the items it names), the tax lines and the amount due.
   It never subtracts a discount or computes a total.
4. The agent calls a `translate_jp_to_en_text` tool as needed for English names.
5. `receipts/allocation.py` does the money math in integer minor units.
   It resolves which items each line applies to from the printed numbers (a printed base amount or percentage), applies the lines in printed order by largest remainder, and reconciles the result against the printed amount due and tax lines.
6. The output validator asks the model to read again only when a transcription cannot be read as money, and at most once more when it does not add up; the run returns the best attempt, not the last.
7. The receipt image is uploaded to Cloudinary; on failure, it is uploaded to catbox.moe.
8. The API returns the items with `cost` set to what each line cost after every discount, plus shop names, tax %, the total paid, date, and image URL.
   A scan that does not reconcile allocates nothing (every `cost` is 0), so the app cannot file a guessed split; the review fields say why.

### Flow 2 — Transaction Creation

1. The client `POST`s the OCR results and member allocations to `POST /api/v1/settle-up/transactions/`.
2. `SettleUpClient` signs in through the Firebase Auth REST API (`settle_up/firebase_auth.py`); the token is cached ~58 min in Redis.
3. Per-member itemized costs are aggregated from the `user_receipt_items` list.
4. Tax is added per member **only if** validation passes: `sum(member taxes + member items + shared taxes) == total_amount` (float equality — all-or-nothing).
5. Shared items are split evenly across all group members (fetched from the Settle Up cache).
6. Integer weights are computed via GCD reduction on the final per-member totals.
7. A single-item transaction is `POST`ed to Settle Up. `whoPaid` contains the full `total_amount` as the payer's weight, while `forWhom` contains the GCD-reduced per-member weights; `currencyCode` is the group's own currency, with an identity `exchangeRates` entry.
8. Settle Up returns the transaction ID.

## Tech Stack

| Component | Purpose |
| --- | --- |
| Django 5.2 LTS | Web framework; Ninja router for async REST endpoints |
| Django Ninja | Async REST framework with schema validation |
| Pydantic | Data validation and serialization for OCR output and API schemas |
| Pydantic AI (`pydantic-ai-slim`) | Agent framework wrapping OpenAI-compatible providers (OpenRouter, OpenAI/LLM7) for structured LLM output |
| django-redis | Redis cache backend for tokens and metadata |
| requests | HTTP client for Firebase Auth, the Settle Up REST API and image uploads |
| cloudinary | Cloudinary Python SDK for image uploads |
| googletrans | Google Translate wrapper for Japanese-to-English translation |

## Prerequisites

- **Python 3.14**, pinned in `.python-version`; `uv sync` downloads it if it is missing
- **[uv](https://docs.astral.sh/uv/)** package manager
- **Redis** (the cache backend is required for token and metadata caching)
- Credentials for the external services used by the app: an LLM API key, Cloudinary credentials, and Settle Up / Firebase credentials (see [Environment variables](#environment-variables))

## Getting Started

### Installation

```bash
# Install all dependencies, including the dev group (pytest, ruff, mypy, ...).
# uv installs the dev dependency-group by default.
uv sync

# Runtime dependencies only (production parity)
uv sync --no-dev

# Install the git pre-commit hook (black + ruff check on every commit)
uv run pre-commit install
```

> Django migrations are optional — the app has no database models (each app's `models.py` is empty and there are no application migrations). Only Django's built-in apps would migrate. Run the following only if you need Django admin or built-in tables:
>
> ```bash
> uv run python manage.py migrate
> ```

### Environment variables

All variables are optional and default to an empty value unless noted otherwise.

| Variable | Required | Description | Default |
| --- | --- | --- | --- |
| `LLM_API_KEY` | No | API key for the OpenAI/LLM7 OCR provider (`get_receipt_agent`) | _(empty)_ |
| `OPENROUTER_API_KEY` | No | API key for the OpenRouter OCR provider (`get_openrouter_receipt_agent`); the OCR endpoint uses this by default | _(empty)_ |
| `OPENROUTER_MODEL` | No | Model id sent to OpenRouter for the OCR flow | `google/gemini-3-flash-preview` |
| `CLOUDINARY_API_SECRET` | No | Cloudinary API secret for image management | _(empty)_ |
| `CLOUDINARY_API_KEY` | No | Cloudinary API key for image management | _(empty)_ |
| `SETTLE_UP_API_KEY` | No | Firebase web API key used to sign the Settle Up account in (Firebase Auth REST) | _(empty)_ |
| `SETTLE_UP_API_DOMAIN` | No | Host of Settle Up's Firebase Realtime Database (e.g. `settle-up-live.firebaseio.com`) | _(empty)_ |
| `SETTLE_UP_USER` | No | Username for Settle Up API authentication | _(empty)_ |
| `SETTLE_UP_PASSWORD` | No | Password for Settle Up API authentication | _(empty)_ |
| `UPSTREAM_HTTP_TIMEOUT` | No | Seconds any single upstream HTTP call (Firebase, Settle Up, image hosts) may take | `30` |
| `REDIS_URL` | No | Connection string for the Redis cache backend (django_redis) | `redis://localhost:6379/1` |
| `APP_AUTH` | No | Bearer token clients must send; when unset, every request is rejected | _(empty)_ |
| `DJANGO_SECRET_KEY` | Yes, in production | Django `SECRET_KEY`; the fallback is an insecure key meant only for local development | _(insecure dev key)_ |

### Running locally

```bash
# Django development server
uv run python manage.py runserver 0.0.0.0:8000

# Production-style ASGI server with Hypercorn
uv run hypercorn --bind 0.0.0.0:8000 settledown.asgi:application
```

The API listens on port **8000**.

### Running with Docker

The simplest way to run the app together with Redis is Docker Compose:

```bash
# Build and start the app together with Redis
docker compose up --build
```

Inside a container, `localhost` is the container itself, so `docker-compose.yml` points the app at the `redis` service with `REDIS_URL: redis://redis:6379/1`.

To build and run the image manually (without Compose), you must create a network and a Redis container, then connect the app to it:

```bash
docker build -t settledown .

docker network create settledown
docker run -d --name redis --network settledown redis:8
docker run -p 8000:8000 --network settledown -e REDIS_URL=redis://redis:6379/1 settledown
```

## API Reference

All endpoints are served under the `/api` prefix and require authentication. The API uses an HTTP Bearer scheme: the `GlobalAuth` class authenticates each request by comparing the supplied token against `settings.APP_AUTH`. Send the token in an `Authorization: Bearer <token>` header.

Interactive Swagger UI is available at **`/api/docs`** and the OpenAPI document at `/api/openapi.json`.
The same document is committed as [`openapi.json`](openapi.json), so every API contract change shows up in review.

### `POST /api/v1/receipts/receipt-items/`

Extract receipt items from an uploaded image using OCR via AI.

**Auth:** Bearer token required.

**Request** (`multipart/form-data`):

| Field | Type | Description |
| --- | --- | --- |
| `file` | File (UploadedFile) | Image file upload |

**Response** (`200`):

| Field | Type | Description |
| --- | --- | --- |
| `receipt_items` | `list[OCRReceiptItemOut]` | Items in printed order, each with `english_name: str`, `japanese_name: str`, `item_order: int` (from 1), `cost: float`, `quantity: int` and `discount: float` |
| `en_shop_name` | `str` | Shop name in English |
| `jp_shop_name` | `str` | Shop name as printed |
| `tax_percentage` | `float` | The printed tax rate (the one with the largest taxable base when several are printed), or 0 |
| `total_amount` | `float` | What the group splits: the amount paid after every discount, coupon and points |
| `receipt_date` | `datetime` | Receipt date |
| `receipt_image_url` | `str` | URL of the uploaded receipt image |

`cost` is what each line cost after its share of every discount, coupon, points and surcharge, so the items add up to `total_amount`.
In the example below, a 15% discount takes 300 off the 2,000 lotion, then 270 of redeemed points come off both lines in proportion (100 and 170).
When tax is added on top at one printed rate, `cost` stays before tax and the items plus `tax_percentage` add up to `total_amount`, as before.
`discount` is everything taken off the printed price, already subtracted from `cost`.
When a scan cannot be reconciled with the printed totals, or a discount's items cannot be settled from the printed numbers, nothing is allocated: every `cost` and `discount` is 0.
Printed prices are not returned as costs because they could add up to a misread total, which an app that checks costs against the total would accept.

The review fields describe the receipt as printed and how it was settled:

| Field | Type | Description |
| --- | --- | --- |
| `receipt_items[].list_price` | `float` | The price printed on the item's own line |
| `decimal_places` | `int` | Decimal places of the receipt's currency (0 for yen, 2 for cents) |
| `adjustments` | `list` | Every printed line that changed the amount to pay, in printed order: `printed_label`, `kind` (`discount`, `points` or `surcharge`), `amount`, `percentage`, `eligible_subtotal`, `item_orders`, `status` and `allocations` |
| `tax_lines` | `list` | The printed tax lines: `rate`, `taxable_amount`, `tax_amount` |
| `reconciliation` | `object` | `status` (`reconciled`, `needs_review` or `mismatch`) and the unexplained `difference` |

An adjustment's `status` is `verified` when printed numbers (a base amount or a percentage) prove which items it covers, `assumed` when it was taken from the receipt's layout or applied to the whole receipt, and `unresolved` when a person must choose its items.
`allocations` lists what each item took, and is empty unless the receipt reconciled.

**Example:**

```bash
curl -X POST "http://localhost:8000/api/v1/receipts/receipt-items/" \
  -H "Authorization: Bearer <APP_AUTH_TOKEN>" \
  -F "file=@receipt.jpg"
```

```json
{
  "receipt_items": [
    {
      "english_name": "Dry shampoo",
      "japanese_name": "ドライシャンプー",
      "item_order": 1,
      "cost": 900.0,
      "quantity": 1,
      "discount": 100.0
    },
    {
      "english_name": "Moisturizing lotion",
      "japanese_name": "保湿ローション",
      "item_order": 2,
      "cost": 1530.0,
      "quantity": 1,
      "discount": 470.0
    }
  ],
  "en_shop_name": "Sample Pharmacy",
  "jp_shop_name": "サンプル薬局",
  "tax_percentage": 10.0,
  "total_amount": 2430.0,
  "receipt_date": "2026-11-23T15:16:00",
  "receipt_image_url": "https://res.cloudinary.com/example/image/upload/receipt.jpg"
}
```

### `POST /api/v1/receipts/allocations/`

Recalculate a scanned receipt after a person chose which items each adjustment applies to.
The app's review step posts back what the scan returned, with each adjustment's `item_orders` set to the chosen items; fields it does not need are ignored.
There is no model call and no image: the same allocation runs with the chosen items, then reconciles against the same printed totals and tax lines.

**Auth:** Bearer token required.

**Request** (`application/json`):

| Field | Type | Description |
| --- | --- | --- |
| `decimal_places` | `int` | As the scan returned it |
| `receipt_items` | `list` | Each with `item_order` and `list_price` |
| `adjustments` | `list` | Each with `printed_label`, `kind`, `amount` and the chosen `item_orders`; `percentage` and `eligible_subtotal` are optional and only echoed |
| `tax_lines` | `list` | The scan's `tax_lines` (optional) |
| `total_amount` | `float` | The scan's `total_amount` |

**Response** (`200`): `receipt_items` (each with `item_order`, `list_price`, `cost`, `discount`), `adjustments`, `reconciliation`, `tax_percentage` and `total_amount`, with the same meaning as in the scan response.
A choice that cannot be applied, such as an unknown item or a discount larger than what its items cost, is a `422` with a `detail` message.

### `GET /api/v1/settle-up/groups/`

Retrieve the list of settle-up groups.

**Auth:** Bearer token required.

**Request:** No parameters.

**Response** (`200`): a list of groups, each with:

| Field | Type | Description |
| --- | --- | --- |
| `name` | `str` | Group name |
| `id` | `str` | Group ID |
| `currency` | `str \| null` | The currency the group settles in (Settle Up `convertedToCurrency`) |

**Example:**

```bash
curl -X GET "http://localhost:8000/api/v1/settle-up/groups/" \
  -H "Authorization: Bearer <APP_AUTH_TOKEN>"
```

```json
[
  { "name": "Apartment", "id": "group-abc123", "currency": "JPY" },
  { "name": "Trip to Taipei", "id": "group-def456", "currency": "TWD" }
]
```

### `GET /api/v1/settle-up/users/`

Retrieve the users in a specific settle-up group.

**Auth:** Bearer token required.

**Request** (query parameters):

| Parameter | Type | Description |
| --- | --- | --- |
| `group_id` | `str` | ID of the group |

**Response** (`200`): a list of users, each with:

| Field | Type | Description |
| --- | --- | --- |
| `name` | `str` | User name |
| `id` | `str` | User ID |

**Example:**

```bash
curl -X GET "http://localhost:8000/api/v1/settle-up/users/?group_id=group-abc123" \
  -H "Authorization: Bearer <APP_AUTH_TOKEN>"
```

```json
[
  { "name": "Member 1", "id": "member-1" },
  { "name": "Member 2", "id": "member-2" }
]
```

### `POST /api/v1/settle-up/transactions/`

Create a new settlement transaction. Transactions are filed in **the group's currency** (the group document's `convertedToCurrency`); amounts are assumed to already be denominated in it.

**Auth:** Bearer token required.

**Request** (`application/json`):

| Field | Type | Description |
| --- | --- | --- |
| `purpose` | `str` | Transaction purpose |
| `paying_member_id` | `str` | ID of the member paying |
| `tax_percentage` | `float` | Finite tax percentage, such as `8.25` or `10` |
| `total_amount` | `float` | Total transaction amount |
| `user_receipt_items` | `list[UserTransactionSchema]` | Receipt items per user (each with `member_id: str`, `cost: float`) |
| `split_receipt_items` | `list[float]` | Optional split items (defaults to an empty list) |
| `group_id` | `str` | Group ID |
| `receipt_date` | `datetime \| None` | Optional receipt date |
| `receipt_image_url` | `str \| None` | Optional receipt image URL |

**Response:** `204 No Content` (empty body) on success.

**Example:**

```bash
curl -X POST "http://localhost:8000/api/v1/settle-up/transactions/" \
  -H "Authorization: Bearer <APP_AUTH_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "purpose": "Sample Mart",
    "paying_member_id": "member-1",
    "tax_percentage": 10,
    "total_amount": 330.0,
    "user_receipt_items": [
      { "member_id": "member-1", "cost": 150.0 },
      { "member_id": "member-2", "cost": 150.0 }
    ],
    "split_receipt_items": [],
    "group_id": "group-abc123",
    "receipt_date": "2026-05-30T12:34:56",
    "receipt_image_url": "https://res.cloudinary.com/example/image/upload/receipt.jpg"
  }'
```

## Testing

Tests mock every external boundary (Firebase Auth, the Settle Up REST API via `requests`, the cache, the OCR agent and the image hosts), so **no live Redis or network access is required**.
The shared test fixture group has exactly two members: **Member 1** and **Member 2**.

```bash
# Run the full test suite
uv run pytest

# With the coverage gate CI enforces (100% of lines and branches)
uv run pytest --cov

# Run a single test module
uv run pytest settle_up/tests/test_transaction.py
```

The suite is strict on purpose:

- **Warnings are errors** (`filterwarnings = ["error"]`), so a deprecation fails the PR that introduces it instead of the upgrade that removes the API.
- **HTTP tests go through the real stack**: each app's `tests/test_api.py` (and `settledown/tests/test_auth.py`) uses Django's test `Client`, so requests traverse the URL conf, middleware and bearer auth.
- **Live tests are opt-in.** Tests marked `live` call paid APIs and are deselected by default; run them with `uv run pytest -m live` (see `test_ocr_agent_live.py` for the keys and receipt image they need).

### API contract snapshot

`openapi.json` must match what the code serves; a test fails when it is stale.
After an intentional API change, regenerate it and review the diff:

```bash
UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest settledown/tests/test_openapi_schema.py
```

### Code quality checks

```bash
uv run black .              # format
uv run ruff check --fix .   # lint, including security (bandit) and Django rules
uv run mypy .               # type check with the django-stubs and pydantic plugins
uv run lint-imports         # architecture contracts (app layers and independence; money math stays pure)
uv run deptry .             # every import is a declared dependency, and vice versa
```

The rule sets and contracts live in `pyproject.toml`.
The import contracts keep the two apps independent of each other and of the `settledown` project package, layer each app as routes > services > integrations > pure logic, and forbid the money math (`receipts.allocation`, `settle_up.splitting`) from importing Django, ninja, HTTP clients or SDKs.

### Pre-commit hooks

A [pre-commit](https://pre-commit.com) config (`.pre-commit-config.yaml`) runs `black --check` and `ruff check` on the staged Python files before each commit.
The hooks shell out to `uv run`, so they reuse the versions pinned in the dev dependency group.
Install the git hook once per clone:

```bash
uv run pre-commit install
```

To run them against every tracked file, as the CI lint job does:

```bash
uv run pre-commit run --all-files
```

## Continuous integration

All workflows live in `.github/workflows/`.
Actions are pinned to exact version tags (for example `astral-sh/setup-uv@v10.3.3`) and container images to digests.
Every job gets read-only repository access unless it needs more.

| Workflow | Trigger | Jobs |
| --- | --- | --- |
| `ci.yml` | Every PR and push to `main` | **Lint** (ruff, actionlint), **Type check** (mypy), **Test** (pytest with the 100% coverage gate), **Django checks** (`check`, `makemigrations --check`), and **CI passed**, which aggregates them all |
| `security.yml` | Every PR, push to `main`, and weekly | **pip-audit** over the hash-locked dependency set |
| `nightly.yml` | Daily | **Latest-dependencies canary**: `uv lock --upgrade`, then the tests and mypy |

Make **CI passed** the required status check for `main` in the branch protection settings: it is green only when every CI job passed.

## Project Structure

```
settledown/
├── manage.py
├── pyproject.toml              # Dependencies and every tool's configuration
├── uv.lock
├── openapi.json                # Committed API contract (kept in sync by a test)
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/           # ci.yml, security.yml, nightly.yml
├── settledown/                 # Django project: wiring only
│   ├── api.py                  # NinjaAPI, GlobalAuth, mounts each app's router
│   ├── settings.py             # Settings (env vars, Redis cache, APP_AUTH)
│   ├── urls.py                 # /api/ -> NinjaAPI
│   ├── asgi.py                 # ASGI entrypoint (settledown.asgi:application)
│   └── tests/                  # Bearer auth, OpenAPI snapshot, entrypoints
├── receipts/                   # App: /api/v1/receipts/
│   ├── api.py                  # Router (routes only): POST /receipt-items/, POST /allocations/
│   ├── schemas.py              # Request/response schemas
│   ├── services.py             # Responses: scan_response, recalculate_receipt
│   ├── ocr.py                  # Pydantic AI receipt agents (OpenRouter, OpenAI/LLM7), read_receipt
│   ├── image_hosting.py        # Photo hosting: Cloudinary, falling back to catbox.moe
│   ├── allocation.py           # Pure receipt money math: discount scope, allocation, reconciliation
│   ├── transcription.py        # ReceiptScan: what the OCR model transcribes
│   ├── llm7.py                 # LLM7ChatModel - LLM response parsing workaround
│   ├── models.py               # Empty - no application ORM models
│   └── tests/                  # Golden vectors, agent loop, routes over HTTP
└── settle_up/                  # App: /api/v1/settle-up/
    ├── api.py                  # Router (routes only): groups, users, transactions
    ├── schemas.py              # Request/response schemas
    ├── services.py             # SettleUpClient: Settle Up REST calls, caching, transaction build
    ├── firebase_auth.py        # Firebase Auth REST sign-in
    ├── splitting.py            # Pure transaction-splitting math (tax inference, weights)
    ├── dto.py                  # Settle Up data structures
    ├── models.py               # Empty - no application ORM models
    └── tests/
        ├── conftest.py         # SettleUpClient with its external boundaries mocked
        └── test_*.py
```

## Notes & Limitations

- **No application database:** each app's `models.py` is empty; all state is external (Firebase, Redis, client).
- **No Django ORM models:** transactions are created only via the Firebase REST API.
- **No models are registered with the Django admin** (the app has no models).
- **Tax is all-or-nothing on transactions:** it is computed only if `member_taxes + member_items + shared_tax == total_amount` (float equality).
- **Scans with a deduction after tax do not reconcile**, nor do tax-on-top receipts at several rates whose printed bases do not identify each rate's items, nor receipts with a surcharge outside the tax base (such as a US gratuity with a printed tax rate); the app asks for review.
- **No FX conversion:** transactions are filed in the group's currency and receipt amounts are assumed to match it; a receipt in a different currency than its group is recorded unconverted.
- **The transaction endpoint trusts `total_amount`:** the scan endpoint reconciles every scan against the printed totals, but a transaction posted directly is not checked.
- **Settle Up metadata is cached ~24h** (~86,500s): group/member info may be stale until the cache expires.
- **Shared items are split evenly** across all members; there are no per-member overrides.
- **A receipt image is required for a transaction** (the URL field is optional, but the OCR flow always provides it).
- **LLM errors raise `ModelRetry`;** translation failures retry with shorter text chunks.

---

deploy v1
