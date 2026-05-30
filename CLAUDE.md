# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the human-facing reference — full setup, the env-var table, the API reference with example payloads, and the architecture diagram. **This file is the agent's operating guide:** the non-obvious, gotcha-level context to read before changing anything financial, infra, or test-related. Don't duplicate the README; consult it for the full picture.

## What this is

A Django + Django-Ninja REST API that turns a photo receipt into a settled [Settle Up](https://settleup.io) group expense. It is a **stateless middleman**, not a typical Django app: `backend_api/models.py` is empty and `backend_api/admin.py` is fully commented out. There is no app-owned data — state lives in Firebase (via Settle Up), Redis (cache), Cloudinary (images), or the client. The SQLite DB exists only for Django's built-in apps and is untouched by the tests.

## Commands

Everything runs through **uv** (Python 3.12+, see `uv.lock`):

```bash
uv sync                                   # install ALL deps incl. dev group (pytest/black/ruff) — uv includes dev by default
uv sync --no-dev                          # runtime deps only (prod parity)

uv run python manage.py runserver         # local dev server (async views work under runserver)
uv run hypercorn settledown.asgi:application --bind 0.0.0.0:8000   # how prod/Docker runs it

uv run pytest                             # run tests (no Redis/network needed — all boundaries mocked)
uv run pytest --cov
uv run pytest backend_api/tests/test_transaction.py::TestTransaction::test_fractional_tax_balances_to_total   # single test

uv run black --check .                        # format check (CI mode; drop --check to apply)
uv run ruff check .                           # lint
```

`uv run python manage.py migrate` is **optional** — there are no app models or migrations, only Django's built-ins. The running app **does** require Redis (`django_redis` cache backend) and the env vars documented in `README.md`; the tests do not.

## Architecture

Request entry is `settledown/api.py`: one `NinjaAPI` protected by `GlobalAuth` (a `HttpBearer` that just compares the token to `settings.APP_AUTH`). It mounts two routers under `/api/`:

- `/api/v1/receipts/receipt-items/` (`backend_api/api.py`) — **the OCR flow**. An async `pydantic-ai` `Agent` running `gpt-5-mini` reads an uploaded image with `output_type=ReceiptData`, so the LLM is forced to return a validated `ReceiptData` (items, EN/JP shop names, tax %, total, date) rather than free text. It has one tool, `translate_jp_to_en_text` (Google Translate). The original image is uploaded to Cloudinary, falling back to catbox.moe on any exception (`backend_api/services.py`). The model is wrapped by `backend_api/dataclasses/llm7_override.py` (`LLM7ChatModel`), a parsing workaround — don't delete it as dead code.
- `/api/v1/settle-up/...` (`backend_api/settleup_api.py`) — list groups, list members, and create a transaction. All delegate to `SettleUpClient`.

Swagger UI is at `/api/docs/` (authorize with the `APP_AUTH` bearer token).

### `SettleUpClient` is the core (`backend_api/settleup_utils.py`)

Constructed fresh per request. `__init__` signs in to Firebase via `pyrebase` and caches the token in Redis (~3500s ≈ 58 min). `get_groups` / `get_group_members_by_group` hit the Settle Up REST API and cache results for ~24h (`timeout=86500`) — **so changes made in Settle Up won't appear until that cache expires.**

The money logic is two methods worth understanding before touching anything financial:

- **`_compute_transaction()`** turns per-member items + shared items + a tax % + the trusted `total_amount` into `{member_id: yen_owed}`. It does not know whether the receipt's printed total already includes consumption tax, so it *infers* it: `should_compute_tax` is an **exact float `==` comparison** of `(pre-tax items + computed tax + shared) == total_amount`. If equal, tax was excluded and gets added to everyone; if not, tax is assumed already baked in and is not added. This is held together by `round(_, 2)` on every tax term — those rounds are load-bearing, not cosmetic (they were the "Fix tax calculation precision" change).
- **`_compute_weights()`** reduces the per-member totals to the smallest integer ratio (`int(round(s*100))` then divide by the GCD). In `create_transaction`, those GCD-reduced weights go into the transaction's `forWhom`, while `whoPaid` carries the **full `total_amount`** as the payer's weight (`settleup_utils.py:186-207`) — don't conflate the two. Currency is hardcoded to `JPY`.

### Constraints baked into the current model (don't assume otherwise)

- Tax is a single scalar applied **all-or-nothing** — mixed rates (e.g. JP 8% food vs 10%) are not representable.
- `total_amount` is **trusted input**, never validated; the OCR prompt merely asks the LLM to make items sum to it.
- The float-`==` tax heuristic means a 1-yen rounding drift can silently flip the entire tax decision. Be careful changing any rounding or the comparison.

## Gotchas

- **`docker compose up` ships a broken Redis config.** `docker-compose.yml` does not set `REDIS_URL`, and `settings.py` defaults it to `redis://localhost:6379/1`. Inside the `app` container `localhost` is the container itself, not the `redis` service, so every cache-backed (i.e. every Settle Up) endpoint fails — though the server still starts, because django-redis connects lazily. Fix: set `environment: REDIS_URL: redis://redis:6379/1` on the `app` service. See README → Running with Docker.
- Image upload (`backend_api/services.py`) **swallows the Cloudinary exception** (only `print`s it) and silently falls back to catbox.moe.

## Testing

`backend_api/tests/conftest.py` provides the `settle_up_client` fixture: a real `SettleUpClient` with `pyrebase`, `requests`, and `cache` all patched, so no login/network/Redis happens. The mocked members endpoint returns exactly **Member 1 and Member 2** (`GROUP_MEMBERS`), and the split-related assertions in `test_transaction.py` depend on that two-member group. Tests exercise `_compute_transaction()` directly across tax-included / tax-excluded / shared-split / fractional-tax scenarios. pytest is configured in `pyproject.toml` (`DJANGO_SETTINGS_MODULE=settledown.settings`, `test_*.py`).

## CI

`.github/workflows/ci.yml` runs two jobs on every pull request and on pushes to `main`: a combined **`lint`** job (`ruff check` then `black --check` in one step) and **`test`** (`pytest`). Keep the lint/format commands here, in CI, and in the README in sync (`black`/`ruff` target `.`).