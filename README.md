# settledown

> Receipt-splitting backend that extracts expenses from photo receipts via OCR and files them into Settle Up groups with tax-aware cost apportionment.

settledown is a Django REST API that turns a photo of a receipt into a settled group expense. It processes receipt images, extracts itemized costs via multimodal LLM analysis (with Japanese-to-English translation), and automatically creates transaction records in [Settle Up](https://settleup.io/). It handles group-based expense splitting with support for shared items, per-person itemization, and all-or-nothing tax computation, caching Settle Up group and member data in Redis to minimize Firebase API calls.

## License

This project does not currently include a LICENSE file. As such, it is **UNLICENSED** — no usage, distribution, or modification rights are granted by default. Add a standard license (MIT, Apache 2.0, etc.) if you intend to make it reusable.

## Features

- OCR receipt image processing with bilingual (Japanese/English) text extraction
- AI-powered line-item parsing with quantity and discount handling
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
   | OpenAI      |      | Google Translate|  |   |  Redis    |      | Firebase /   |
   | (GPT-5-mini)|      | (googletrans)   |  |   |  cache    |      | Settle Up    |
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
2. The endpoint initializes a Pydantic AI agent backed by the OpenRouter provider (default `google/gemini-2.5-flash-lite`, via `get_openrouter_receipt_agent`). An alternate OpenAI/LLM7 factory (`get_receipt_agent`) remains available.
3. The agent receives a system prompt (extract items, bilingual shop names, tax %, total) plus the image.
4. The agent calls a `translate_jp_to_en_text` tool as needed (the system prompt instructs it to always translate the text to English before processing, so the tool may be invoked unconditionally).
5. The LLM validates that the extracted items sum to the declared total (all-or-nothing check).
6. The receipt image is uploaded to Cloudinary; on failure, it is uploaded to catbox.moe.
7. The API returns the receipt data: items list, shop names, tax %, total, date, and image URL.

### Flow 2 — Transaction Creation

1. The client `POST`s the OCR results and member allocations to `POST /api/v1/settle-up/transactions/`.
2. `SettleUpClient` initializes Firebase auth (token cached ~58 min via Redis).
3. Per-member itemized costs are aggregated from the `user_receipt_items` list.
4. Tax is added per member **only if** validation passes: `sum(member taxes + member items + shared taxes) == total_amount` (float equality — all-or-nothing).
5. Shared items are split evenly across all group members (fetched from the Settle Up cache).
6. Integer weights are computed via GCD reduction on the final per-member totals.
7. A single-item transaction is `POST`ed to Settle Up. `whoPaid` contains the full `total_amount` as the payer's weight, while `forWhom` contains the GCD-reduced per-member weights; `currencyCode` is `JPY`.
8. Settle Up returns the transaction ID.

## Tech Stack

| Component | Purpose |
| --- | --- |
| Django 5.2 | Web framework; Ninja router for async REST endpoints |
| Django Ninja | Async REST framework with schema validation |
| Pydantic | Data validation and serialization for OCR output and API schemas |
| Pydantic AI | Agent framework wrapping OpenAI-compatible providers (OpenRouter, OpenAI/LLM7) for structured LLM output |
| Pyrebase | Firebase Realtime Database client for the Settle Up API |
| django-redis | Redis cache backend for tokens and metadata |
| requests | HTTP client for the Settle Up REST API and image uploads |
| cloudinary | Cloudinary Python SDK for image uploads |
| googletrans | Google Translate wrapper for Japanese-to-English translation |

## Prerequisites

- **Python 3.12 or higher**
- **[uv](https://docs.astral.sh/uv/)** package manager
- **Redis** (the cache backend is required for token and metadata caching)
- Credentials for the external services used by the app: an LLM API key, Cloudinary credentials, and Settle Up / Firebase credentials (see [Environment variables](#environment-variables))

## Getting Started

### Installation

```bash
# Install all dependencies, including the dev group (pytest, black, ruff). uv installs
# the dev dependency-group by default.
uv sync

# Runtime dependencies only (production parity)
uv sync --no-dev

# Install the git pre-commit hook (black + ruff checks on every commit)
uv run pre-commit install
```

> Django migrations are optional — the app has no database models (`backend_api/models.py` is empty and there are no application migrations). Only Django's built-in apps would migrate. Run the following only if you need Django admin or built-in tables:
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
| `OPENROUTER_MODEL` | No | Model id sent to OpenRouter for the OCR flow | `google/gemini-2.5-flash-lite` |
| `CLOUDINARY_API_SECRET` | No | Cloudinary API secret for image management | _(empty)_ |
| `CLOUDINARY_API_KEY` | No | Cloudinary API key for image management | _(empty)_ |
| `SETTLE_UP_API_KEY` | No | API key for Settle Up Firebase authentication | _(empty)_ |
| `SETTLE_UP_API_DOMAIN` | No | Domain for the Settle Up API Firebase auth domain and database URL | _(empty)_ |
| `SETTLE_UP_API_NAMESPACE` | No | Firebase project namespace for storage bucket and project ID | _(empty)_ |
| `SETTLE_UP_USER` | No | Username for Settle Up API authentication | _(empty)_ |
| `SETTLE_UP_PASSWORD` | No | Password for Settle Up API authentication | _(empty)_ |
| `REDIS_URL` | No | Connection string for the Redis cache backend (django_redis) | `redis://localhost:6379/1` |
| `APP_AUTH` | No | Application authentication token or secret (validated by the Bearer auth scheme) | _(empty)_ |

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

> **Important:** Inside a container, `localhost` refers to the container itself, not the Redis service. The default `REDIS_URL` (`redis://localhost:6379/1`, from `settings.py`) will therefore fail to reach the `redis` container. The `docker-compose.yml` currently does **not** set a `REDIS_URL` override, so the app service must be configured to point at the `redis` service. Add the following to the `app` service in `docker-compose.yml`:
>
> ```yaml
>   app:
>     # ...
>     environment:
>       REDIS_URL: redis://redis:6379/1
> ```

To build and run the image manually (without Compose), you must create a network and a Redis container, then connect the app to it:

```bash
docker build -t settledown .

docker network create settledown
docker run -d --name redis --network settledown redis:latest
docker run -p 8000:8000 --network settledown -e REDIS_URL=redis://redis:6379/1 settledown
```

## API Reference

All endpoints are served under the `/api` prefix and require authentication. The API uses an HTTP Bearer scheme: the `GlobalAuth` class authenticates each request by comparing the supplied token against `settings.APP_AUTH`. Send the token in an `Authorization: Bearer <token>` header.

Interactive Swagger UI is available at **`/api/docs/`**.

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
| `receipt_items` | `list[ReceiptItemData]` | List of receipt items (each with `english_name: str`, `japanese_name: str`, `item_order: int`, `cost: float`, `quantity: int`, `discount: int`) |
| `en_shop_name` | `str` | Shop name in English |
| `jp_shop_name` | `str` | Shop name in Japanese |
| `tax_percentage` | `float` | Tax percentage |
| `total_amount` | `float` | Total receipt amount |
| `receipt_date` | `datetime` | Receipt date |
| `receipt_image_url` | `str` | URL of the uploaded receipt image |

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
      "english_name": "Onigiri",
      "japanese_name": "おにぎり",
      "item_order": 1,
      "cost": 150.0,
      "quantity": 2,
      "discount": 0
    }
  ],
  "en_shop_name": "Sample Mart",
  "jp_shop_name": "サンプルマート",
  "tax_percentage": 10.0,
  "total_amount": 330.0,
  "receipt_date": "2026-05-30T12:34:56",
  "receipt_image_url": "https://res.cloudinary.com/example/image/upload/receipt.jpg"
}
```

### `GET /api/v1/settle-up/groups/`

Retrieve the list of settle-up groups.

**Auth:** Bearer token required.

**Request:** No parameters.

**Response** (`200`): a list of groups, each with:

| Field | Type | Description |
| --- | --- | --- |
| `name` | `str` | Group name |
| `id` | `str` | Group ID |

**Example:**

```bash
curl -X GET "http://localhost:8000/api/v1/settle-up/groups/" \
  -H "Authorization: Bearer <APP_AUTH_TOKEN>"
```

```json
[
  { "name": "Apartment", "id": "group-abc123" },
  { "name": "Trip to Kyoto", "id": "group-def456" }
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

Create a new settlement transaction. Transactions are created in Settle Up with the currency hardcoded to **JPY**.

**Auth:** Bearer token required.

**Request** (`application/json`):

| Field | Type | Description |
| --- | --- | --- |
| `purpose` | `str` | Transaction purpose |
| `paying_member_id` | `str` | ID of the member paying |
| `tax_percentage` | `int` | Tax percentage |
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

Tests mock every external boundary (`SettleUpClient` touches Firebase/pyrebase auth, the Settle Up REST API via `requests`, and the cache), so **no live Redis or network access is required**. The shared test fixture group has exactly two members: **Member 1** and **Member 2**.

```bash
# Run the full test suite
uv run pytest

# Run a single test module
uv run pytest backend_api/tests/test_transaction.py
```

### Linting and formatting

```bash
uv run black .
uv run ruff check .
uv run ruff check --fix .
```

### Pre-commit hooks

A [pre-commit](https://pre-commit.com) config (`.pre-commit-config.yaml`) runs the
same checks as CI — `black --check` and `ruff check` over the repo — before each
commit, blocking the commit if either fails. The hooks shell out to `uv run`, so
they reuse the versions pinned in the dev dependency group (no separate version to
keep in sync). Install the git hook once per clone:

```bash
uv run pre-commit install
```

After that, the checks run automatically on `git commit`. To run them against the
whole tree on demand:

```bash
uv run pre-commit run --all-files
```

## Project Structure

```
settledown/
├── manage.py
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── docker-compose.yml
├── settledown/                 # Django project
│   ├── api.py                  # NinjaAPI instance, GlobalAuth, router registration
│   ├── settings.py             # Settings (env vars, Redis cache, APP_AUTH)
│   ├── urls.py                 # /api/ -> NinjaAPI
│   └── asgi.py                 # ASGI entrypoint (settledown.asgi:application)
└── backend_api/
    ├── api.py                  # Receipts router: POST /receipt-items/ (OCR)
    ├── settleup_api.py         # Settle-up router: groups, users, transactions
    ├── settleup_utils.py       # SettleUpClient: Firebase auth, transaction build
    ├── serializer.py           # Request/response schemas
    ├── services.py             # Cloudinary + catbox.moe image upload
    ├── dto/                    # Data structures
    │   ├── receipt_item.py     # ReceiptData and related OCR structures
    │   ├── settleup.py         # Settle Up data structures
    │   └── llm7_override.py    # LLM7ChatModel — LLM response parsing workaround
    ├── models.py               # Empty — no application ORM models
    ├── admin.py                # Disabled (commented out)
    └── tests/
        ├── conftest.py         # Fixtures with mocked external boundaries
        └── test_transaction.py
```

## Notes & Limitations

- **No application database:** `models.py` is empty; all state is external (Firebase, Redis, client).
- **No Django ORM models:** transactions are created only via the Firebase REST API.
- **Django admin is disabled** (`admin.py` is commented out).
- **Tax is all-or-nothing:** it is computed only if `member_taxes + member_items + shared_tax == total_amount` (float equality).
- **Currency is hardcoded to JPY** in transactions.
- **`total_amount` is trusted input:** there is no reconciliation if extracted items differ from the declared total.
- **Settle Up metadata is cached ~24h** (~86,500s): group/member info may be stale until the cache expires.
- **Shared items are split evenly** across all members; there are no per-member overrides.
- **A receipt image is required for a transaction** (the URL field is optional, but the OCR flow always provides it).
- **LLM errors raise `ModelRetry`;** translation failures retry with shorter text chunks.

---

deploy v1