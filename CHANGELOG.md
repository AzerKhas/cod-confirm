# Changelog

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
