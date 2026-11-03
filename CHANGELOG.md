# Changelog

## Phase 2 – Order intake
- `services/phone.py`: Tunisian mobile numbers -> E.164 `+216XXXXXXXX` (spaces/dots/dashes, `00216`, `216`, Arabic-Indic digits, invisible RTL marks); landlines and foreign numbers rejected.
- `OrderIn` schema (strict lengths, money rounded to millimes, total computed from items when missing).
- `services/intake.py`: idempotent `create_order` (savepoint handles concurrent duplicates), customer reused by phone per shop, Shopify / WooCommerce payload mappers.
- `POST /api/orders` authenticated by `X-API-Key` (SHA-256 lookup); 201 created / 200 duplicate.
- `POST /webhooks/shopify/{slug}` (`X-Shopify-Hmac-Sha256`) and `POST /webhooks/woocommerce/{slug}` (`X-WC-Webhook-Signature`): constant-time HMAC check, 1 MB body limit, topic filter, paid / non-COD orders ignored, bad data answered `200 rejected` so platforms don't retry forever, WooCommerce ping handled.
- Hosted form `GET/POST /form/{slug}` (Jinja2 + Tailwind, Darija / Arabic RTL / French, 24 governorates, prices taken from the shop catalog never from the form, honeypot, Post/Redirect/Get).
- `scripts/create_shop.py` to create a shop and print its API key once.
- Migration `0002` (shop webhook secrets, form products). 201 tests.

## Phase 1 – Models & state machine
- Models `Shop`, `Customer`, `Order`, `MessageLog`, `Event` (+ `app/enums.py`); portable string enums, money as `Numeric(12,3)` (TND millimes), tz-aware UTC datetimes (naive datetimes rejected).
- Intake API keys stored as SHA-256 hash only (`generate_api_key` / `verify_api_key`).
- Unique customer phone per shop; unique `(shop, source, external_id)` so a Shopify/Woo order is never imported twice.
- `services/orders.py`: validated state machine (graph + actor permissions), every change logged as `Event`; refusals update `risk_score` (warning badge at ≥ 50, never auto-block).
- `services/privacy.py`: `delete_customer` (cascade orders/messages/events) and `purge_expired` (per-shop `data_retention_days`, active orders never purged).
- Alembic migration `0001_initial_schema`; test checks models and migrations stay in sync, and downgrade works.
- 125 tests (every valid and invalid transition is tested).

## Phase 0 – Setup
- Project skeleton (`app/`, `routers/`, `services/`, `templates/`, `tests/`, `alembic/`).
- Typed settings with `pydantic-settings` (`.env`), secrets as `SecretStr`; prod refuses to start with placeholder secrets.
- SQLAlchemy 2 engine/session (SQLite dev, PostgreSQL prod), SQLite foreign keys enabled.
- Alembic wired to app metadata and `DATABASE_URL` (batch mode for SQLite).
- Structured JSON logging + `mask_phone` / `mask_secret` helpers.
- `GET /health` (checks DB, returns 503 when DB is down).
- Dockerfile (non-root, healthcheck, runs migrations on start) + docker-compose (app + Postgres).
- pytest suite: health, config validation, masking, JSON logs, Alembic upgrade.
