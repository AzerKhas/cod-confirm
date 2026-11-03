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
| 2 | Order intake (API, Shopify, WooCommerce, form) | ✅ |
| 3 | WhatsApp Cloud API + webhook | ✅ |
| 4 | Conversation logic (intent, i18n, opt-out, 24h window) | ✅ |
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

## Create a shop

```powershell
python -m scripts.create_shop --name "Skmei Tunisie" --slug skmei-tn `
    --product "Skmei 1251=59" --product "Skmei 1335=69"
```

Prints the shop's intake API key **once** (only its hash is stored).

## Receiving orders

| Source | Endpoint | Auth |
|---|---|---|
| Any system | `POST /api/orders` (JSON, see `/docs`) | header `X-API-Key: cc_...` |
| Shopify | `POST /webhooks/shopify/{slug}` – topic `orders/create` | `X-Shopify-Hmac-Sha256` with the shop's `shopify_webhook_secret` |
| WooCommerce | `POST /webhooks/woocommerce/{slug}` – topic `order.created` | `X-WC-Webhook-Signature` with the shop's `woocommerce_webhook_secret` |
| Facebook / Instagram | hosted form `https://<host>/form/{slug}` (`?lang=ar` / `fr`) | public, honeypot |

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/orders -H "X-API-Key: cc_..." -H "Content-Type: application/json" \
  -d '{"external_id":"A-1","customer_name":"Ahmed","phone":"98 123 456","address":"Rue X","city":"Sousse","items":[{"name":"Skmei 1251","qty":1,"price":"59"}]}'
```

Phone numbers are normalized to `+216XXXXXXXX`; only Tunisian mobiles (2x, 4x, 5x, 9x) are accepted.
Shopify paid orders and WooCommerce non-COD orders are ignored. Re-sent webhooks never create duplicates.

## WhatsApp

- Setup of the Meta app, numbers and tokens: [docs/meta-setup.md](docs/meta-setup.md)
- Exact templates to submit (Darija / Arabic / French): [docs/whatsapp-templates.md](docs/whatsapp-templates.md)
- Webhook URL for Meta: `https://<your-domain>/webhook/meta`
- `DRY_RUN=true` logs messages instead of sending them.

## Conversation

| Customer writes | Bot does |
|---|---|
| `ey`, `behi`, `oui`, `نعم`, `1`, ✅, button *Nconfirmi* | order → CONFIRMED, thank-you |
| `la`, `ma n7ebch`, `annuler`, `لا`, `3`, ❌ | order → CANCELLED, owner alerted |
| `n7eb nbaddel l adresse`, `modifier`, `تعديل`, `2` | order → MODIFY_REQUESTED, asks what to change, owner alerted |
| `9adech l livraison?` | owner alerted, "the shop will answer" |
| `STOP`, `ma t3awedch`, `توقف` | opted out forever (until `START`) |
| anything else | clarification with buttons (max 2), then the owner |

Owner alerts go to Telegram: create a bot with @BotFather → `TELEGRAM_BOT_TOKEN`, and set the
owner's chat id in `shops.owner_telegram_chat_id`.

Optional LLM fallback for replies the rules don't understand:
`pip install -r requirements-llm.txt`, then `INTENT_LLM_ENABLED=true` and `ANTHROPIC_API_KEY=...`.

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
