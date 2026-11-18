# COD Confirm

Automatic WhatsApp confirmation of **Cash-On-Delivery** orders for e-commerce shops in Tunisia and the Maghreb.

Each new order → the customer gets a WhatsApp message (official **Meta WhatsApp Cloud API** only) with
✅ Confirmer / ✏️ Modifier / ❌ Annuler → reminders → delivery tracking → dashboard with confirmation,
cancellation, no-answer and refusal rates.

Customer languages: **French, Arabic, English and Tunisian Darija (Latin letters)**. The public order
form opens in French with a one-tap language switcher; the bot then writes in the language the
customer ordered in, and switches automatically to the language the customer replies in.

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
| 5 | Scheduler (reminders, quiet hours) | ✅ |
| 6 | Dashboard | ✅ |
| 7 | Google Sheets sync | ✅ |
| 8 | Multi-tenant & admin | ✅ |
| 9 | Security hardening | ✅ |
| 10 | Deployment | ✅ |

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

## Production

See [DEPLOY.md](DEPLOY.md) (VPS with Caddy HTTPS + PostgreSQL + backups, Render, Railway).

## Docker (app + PostgreSQL, local)

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

## Dashboard

```powershell
python -m scripts.create_user --email owner@shop.tn --shop skmei-tn
```

Then open <http://127.0.0.1:8000/login>. Demo data for a sales demo:

```powershell
python -m scripts.seed_demo     # shop "Boutique Yasmine", login demo@codconfirm.tn
```

## Operator (super-admin)

```powershell
python -m scripts.create_user --email you@codconfirm.tn --role superadmin
```

`/admin` lists every shop with its plan and monthly usage, creates new shops (API key and the
owner's first password are shown once), manages subscriptions (suspend / plan / paid until) and
stores WhatsApp / Shopify / WooCommerce credentials.

| Plan | Orders / month | Price |
|---|---:|---:|
| Trial | 50 | 0 DT |
| Starter | 300 | 49 DT |
| Pro | 1 500 | 129 DT |
| Business | 6 000 | 349 DT |

Above the quota (+10%), orders are still saved but the bot stops messaging until the next
month or an upgrade.

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

**Which language does the bot speak?** The customer's own language:

1. First message (WhatsApp template): the language chosen on the order form (French by default),
   or the shop's default language for Shopify / WooCommerce / API orders.
2. Every reply of the customer is analysed: Arabic letters → Arabic (MSA, understood by everyone,
   also used when the customer writes Tunisian in Arabic letters); Latin letters with Tunisian words
   or digits (`n7eb`, `3andi`, `ey`, `behi`) → Tunisian Darija; French words → French; English
   words → English. The bot switches and keeps that language for the next messages and reminders.
3. Short neutral replies (`ok`, `1`, 👍) never change the language.

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

## Scheduler

Runs inside the app (`SCHEDULER_ENABLED=true`) every minute. **Enable it on one instance only.**

- New order → confirmation right away, except during quiet hours (22:00-08:00 shop time) → sent at 08:00.
- No answer → reminder after 3 h, second after 24 h (`shops.reminder_delays`), then NO_ANSWER 24 h later.
- Every evening (21:00) → summary to the owner on Telegram + purge of data older than `shops.data_retention_days`.

## Subscriptions & payments

Sellers pay their plan in **Dashboard → Abonnement** (1, 3 or 12 months; 12 months = 10 paid)
with **Konnect** (bank card, e-DINAR, wallet) or **Flouci** (Flouci wallet, bank card).

- Configure `KONNECT_API_KEY` + `KONNECT_WALLET_ID` and/or `FLOUCI_PUBLIC_KEY` + `FLOUCI_PRIVATE_KEY`,
  and `PUBLIC_BASE_URL=https://your-domain`. Start with `KONNECT_SANDBOX=true`.
- The price is computed on the server; the payment is always re-verified with the provider's API
  (redirects and webhooks are never trusted) and applied once (receipt `CC-YYYY-000123`).
- New shops get a free trial (`TRIAL_DAYS`, 14). Reminders 3 days before and on the expiry day;
  `BILLING_GRACE_DAYS` (3) after expiry the shop is suspended (no messages, data kept) and comes
  back as soon as it pays.
- Bank transfer / cash: `/admin` → shop → *Enregistrer un paiement manuel*.
- Without keys (dev), a **demo** checkout simulates payments; it is disabled in production.

## Google Sheets (optional)

1. Google Cloud console → create a project → enable the **Google Sheets API** → create a
   **service account** → download its JSON key → `GOOGLE_SERVICE_ACCOUNT_FILE=/path/key.json`.
2. The seller shares their Google Sheet with the service-account email (**Editor**) and
   pastes the sheet id in **Settings**. Orders appear in the tab *Commandes* within a minute.

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

## Security

See [SECURITY.md](SECURITY.md): data kept, controls, key rotation, backups, penetration-test checklist.

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
