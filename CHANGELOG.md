# Changelog

## Phase 0 – Setup
- Project skeleton (`app/`, `routers/`, `services/`, `templates/`, `tests/`, `alembic/`).
- Typed settings with `pydantic-settings` (`.env`), secrets as `SecretStr`; prod refuses to start with placeholder secrets.
- SQLAlchemy 2 engine/session (SQLite dev, PostgreSQL prod), SQLite foreign keys enabled.
- Alembic wired to app metadata and `DATABASE_URL` (batch mode for SQLite).
- Structured JSON logging + `mask_phone` / `mask_secret` helpers.
- `GET /health` (checks DB, returns 503 when DB is down).
- Dockerfile (non-root, healthcheck, runs migrations on start) + docker-compose (app + Postgres).
- pytest suite: health, config validation, masking, JSON logs, Alembic upgrade.
