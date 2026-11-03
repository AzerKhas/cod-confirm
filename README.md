# COD Confirm

Automatic WhatsApp confirmation of **Cash-On-Delivery** orders for e-commerce shops in Tunisia and the Maghreb.

Each new order → the customer gets a WhatsApp message (official **Meta WhatsApp Cloud API** only) with
✅ Confirmer / ✏️ Modifier / ❌ Annuler → reminders → delivery tracking → dashboard with confirmation,
cancellation, no-answer and refusal rates.

Customer languages: **Tunisian Darija (Latin letters)**, Arabic, French — default chosen per shop.

> Tone of the Darija messages: natural, everyday Tunisian as people really write it on WhatsApp
> (e.g. "Salam Ahmed 👋 commande mte3ek wslet, tconfirmi?"), short, polite, no stiff/translated phrasing.

## Status

| Phase | Content | State |
|------:|---------|-------|
| 0 | Setup: skeleton, config, DB, Alembic, Docker, `/health`, tests | ✅ |
| 1 | Models & order state machine | ✅ |
| 2 | Order intake (API, Shopify, WooCommerce, form) | ⏳ |
| 3 | WhatsApp Cloud API + webhook | ⏳ |
| 4 | Conversation logic (intent, i18n, opt-out, 24h window) | ⏳ |
| 5 | Scheduler (reminders, quiet hours) | ⏳ |
| 6 | Dashboard | ⏳ |
| 7 | Google Sheets sync | ⏳ |
| 8 | Multi-tenant & admin | ⏳ |
| 9 | Security hardening | ⏳ |
| 10 | Deployment | ⏳ |

## Run locally (Windows / PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env          # then edit the values
alembic upgrade head
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/health> and <http://127.0.0.1:8000/docs>.

Linux / macOS: same, with `python3.12 -m venv .venv && source .venv/bin/activate` and `cp`.

## Tests

```powershell
pytest
ruff check .
```

## Docker (app + PostgreSQL)

```powershell
copy .env.example .env
docker compose up --build
```

The container runs `alembic upgrade head` then starts uvicorn on port 8000.

## Database migrations

```powershell
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

## Configuration

All settings come from environment variables / `.env` (see `.env.example`). Secrets are never
hardcoded and never logged; phone numbers and tokens are masked in logs.

- `DRY_RUN=true` (default): WhatsApp messages are logged, not sent — no Meta credentials needed.
- `APP_ENV=prod` refuses to start with a weak `SECRET_KEY` or without `META_APP_SECRET` when sending for real.

## Project structure

```
app/
  main.py            FastAPI factory
  config.py          settings (.env)
  db.py              engine, session, Base
  models.py          ORM models (Phase 1)
  schemas.py         Pydantic schemas
  security.py        masking, signatures, hashing
  logging_config.py  JSON logs
  services/          whatsapp, messages, orders, intent, sheets, scheduler
  routers/           health, webhook_meta, intake, dashboard, admin
  templates/         Jinja2 HTML
alembic/             migrations
tests/
```
