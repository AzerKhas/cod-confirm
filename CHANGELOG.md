# Changelog

## Dashboard redesign
- Self-hosted design system (`app/static/app.css`): no CSS framework or CDN, light + dark mode (OS setting or toggle, remembered), responsive (sidebar becomes a drawer on phones), inline SVG icons (`app/icons.py`), favicon.
- Stricter CSP: `style-src 'self'` and `script-src 'self'` + HTMX with SRI only — no Tailwind CDN, no inline styles or scripts (tested).
- Home: 6 KPI tiles with change vs the previous period (confirmation, orders, cancellation, no answer, refusals, money saved), stacked "orders per day by outcome" chart (server-side SVG, validated CVD-safe palette for both themes, hover tooltip, legend, data table view), outcome share bar, "to handle" list with risk badges.
- Orders: quick status chips, live search, avatars, relative dates, clickable rows. Order page: customer card with call / WhatsApp buttons and risk, WhatsApp-style conversation (day separators, sent / delivered / read ticks), timeline, inline edit.
- Settings in sections, toasts for every action, new login page, admin pages and public order form redesigned (trust badges, Arabic RTL fixed: the hidden anti-bot field no longer widens RTL pages).
- 466 tests.

## Phase 10 – Deployment
- `docker-compose.prod.yml`: Caddy (automatic HTTPS, Let's Encrypt, body limit, JSON logs) → app (read-only filesystem, no capabilities, no-new-privileges, single process with the scheduler) → PostgreSQL 16 on an internal network (no published port) + nightly encrypted backups; rotated logs.
- `deploy/Caddyfile` (validated with `caddy validate`), `render.yaml` blueprint, `DEPLOY.md` (VPS step by step, Render, Railway, updates, monitoring, backups off-site, demo).
- Monitoring: ERROR logs pushed to the operator's Telegram (throttled, background thread), optional Sentry without personal data, `/health` for uptime monitors.
- `DATABASE_URL` from Render / Railway (`postgres://`) normalized to psycopg 3; health checks allowed on localhost with `ALLOWED_HOSTS`.
- Verified end to end with Docker: image build from `requirements.lock`, all migrations on PostgreSQL 16, health check, intake → confirmation → scheduler on Postgres, demo seed refused in prod.
- `scripts/seed_demo.py`: demo clothing shop with realistic fake orders and conversations for sales demos. 461 tests.

## Phase 9 – Security hardening
- Webhook secrets (Shopify / WooCommerce) encrypted at rest like WhatsApp tokens; legacy plain values still work; key rotation with `ENCRYPTION_KEY=new,old` + `scripts/rotate_secrets.py`.
- Public order form: signed expiring token (2 h) against scripted submissions (on top of honeypot + per-IP rate limit).
- Security headers on every response (CSP without inline scripts, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, COOP, HSTS in prod, no-store on private pages); inline `onsubmit` replaced by `data-confirm` + `/static/app.js`.
- Global body-size limit (also for chunked bodies), `TrustedHostMiddleware` (`ALLOWED_HOSTS` mandatory in prod).
- `pip-audit` in dev requirements (0 known vulnerabilities), pinned `requirements.lock` used by Docker.
- `deploy/backup.sh` (pg_dump, optional AES-256 encryption, retention), `SECURITY.md` (data kept, controls, secrets & key rotation, backups/restore, incident response, penetration-test checklist).
- 454 tests.

## Phase 8 – Multi-tenant & admin
- Plans (`app/plans.py`): Trial 50 / Starter 300 / Pro 1500 / Business 6000 orders per month (49 / 129 / 349 DT), API rate per plan.
- `UsageCounter` per shop and month (orders, messages sent), kept even when data is purged; duplicates not counted.
- Quotas: over the plan +10%, orders are still saved but no WhatsApp message is sent (no lost orders); suspended shops: API 403, form 404, webhooks ignored, nothing sent.
- Rate limiting (`app/ratelimit.py`, sliding window): intake API per shop and plan, public form 10 POST/min per IP, login 20/min per IP (plus the per-email throttle).
- Super-admin pages `/admin`: all shops with plan, monthly usage vs quota, WhatsApp status, MRR; create a shop + owner (API key and initial password shown once), subscription (plan, paid until, suspend), WhatsApp / Shopify / WooCommerce credentials (encrypted / never displayed, empty field keeps the stored value), regenerate API key, add staff, reset a password (logs the user out everywhere).
- Migration `0008`. 432 tests (tenant isolation, access control, quotas, rate limits).

## Phase 7 – Google Sheets sync (optional per shop)
- `services/sheets.py`: one row per order in the shop's sheet (tab "Commandes", created with a header if missing), updated in place on every change.
- Outbox design: every status change / edit flags the order (`sheet_dirty`); the scheduler pushes up to 50 rows per tick with 3 API calls max. Google errors never block WhatsApp and are retried.
- Values written with `value_input_option=RAW` (no formula injection).
- One service account for the SaaS (`GOOGLE_SERVICE_ACCOUNT_FILE`); the seller shares the sheet with its email and pastes the sheet id in Settings (changing it re-syncs every order).
- Migration `0007`. 415 tests.

## Phase 6 – Dashboard
- `User` model (owner / staff / superadmin), scrypt password hashing, signed session cookie (HttpOnly, SameSite=Lax, Secure in prod) with `session_version` revocation, session renewed on login, login throttling (5 failures / 15 min), no user enumeration (constant-time dummy hash).
- CSRF token on every dashboard POST (form field or `X-CSRF-Token` header for HTMX).
- Pages (Jinja2 + HTMX + Tailwind, responsive): KPIs (today / 7 / 30 days: confirmation, cancellation, no-answer, refusal rates, delivery fees saved), "to handle" list, today's orders, orders with live search + status filter + pagination, order detail with WhatsApp conversation, timeline, risk badge, call / WhatsApp links.
- Actions: confirm by phone, cancel, ship (sends the "order shipped" template, postponed by the scheduler during quiet hours), delivered, refused (updates risk score), edit order (a MODIFY_REQUESTED order goes back to the customer for confirmation), re-send confirmation, delete a customer's data (owner only).
- CSV export (UTF-8 BOM for Excel, `;` separator, formula-injection safe), shop settings page (language, delivery fee, reminder delays, retention, Telegram, form catalog).
- Strict tenant isolation: another shop's order / customer is a 404 (tested).
- `scripts/create_user.py`, `scripts/seed_demo.py` (demo clothing shop with realistic fake data). Migration `0006`. 410 tests.

## Phase 5 – Scheduler
- `services/scheduler.py`: `tick()` every `SCHEDULER_INTERVAL_SECONDS` (APScheduler, started in the app lifespan, single instance), idempotent jobs, one shop's error never blocks the others.
- Pending confirmations: night orders, failed sends and missed background jobs are sent at the next allowed time; gives up after `MAX_SEND_FAILURES`.
- Reminders after `shop.reminder_delays` (default 3h and 24h), skipped while the customer is talking; NO_ANSWER after `NO_ANSWER_AFTER_HOURS` past the last reminder; opted-out customers never reminded.
- Quiet hours 22:00-08:00 local (shop timezone, default Africa/Tunis, `tzdata` bundled for Windows): no business-initiated message at night, including the confirmation right after an order.
- Daily summary to the owner at `DAILY_SUMMARY_HOUR` (orders, confirmation rate, cancellations, no answer, pending, modifications, delivery fees saved) + data-retention purge, once per local day.
- `services/stats.py`: KPIs shared with the dashboard. Migration `0005`.

## Phase 4 – Conversation logic
- `services/intent.py`: rule-based intent detection for Tunisian Darija (Latin + digits), Arabic and French: normalization (accents, Arabic diacritics/letter forms, Arabic-Indic digits, stretched letters), button payloads, 1/2/3 shortcuts, emojis, negations (`ma nconfirmich`, `je confirme pas`), weak words (`la`, `non`, `ok`) only counted in short replies, questions detected.
- Optional LLM fallback (`INTENT_LLM_ENABLED`, official `anthropic` SDK, `requirements-llm.txt`) only when the rules return UNKNOWN; errors / refusals fall back safely.
- `services/conversation.py`: CONFIRM / CANCEL / MODIFY / QUESTION / STOP / START handling with replies in the customer's language (Arabic detected automatically), state machine transitions as `customer`, cancel after shipping refused, free text after a modification request forwarded once, max 2 clarifications (with buttons) then the owner takes over.
- Opt-out: `STOP`, `ma t3awedch`, `توقف`, `arrête`... → never messaged again (templates included), one final acknowledgment; `START` opts back in.
- 24h customer-service window enforced for every free-form reply.
- `services/alerts.py`: owner alerts through a Telegram bot (`TELEGRAM_BOT_TOKEN` + `shops.owner_telegram_chat_id`).
- Migration `0004`. 368 tests (≈100 real replies in Darija / Arabic / French).

## Phase 3 – WhatsApp Cloud API
- `services/whatsapp.py`: official Cloud API client (`send_template`, `send_text`, `send_buttons`, `mark_as_read`), exponential-backoff retries on network errors / 429 / 5xx / Meta transient codes, permanent errors raised at once (24h-window error detected), no token or full phone in errors or logs.
- `DRY_RUN` mode: nothing sent, payload logged (phone masked), fake `dryrun.<uuid>` id; whole flow testable without Meta.
- Shop access tokens encrypted at rest (`app/crypto.py`, Fernet, `ENCRYPTION_KEY` required in prod).
- `services/messages.py`: generic templates for any kind of shop (`order_confirmation`, `order_reminder`, `order_shipped`) in Darija / Arabic / French with quick-reply buttons (payload `CONFIRM:<id>` / `MODIFY:<id>` / `CANCEL:<id>`) and STOP footer; tests enforce Meta's rules.
- `services/notify.py`: sends the confirmation right after a new order (background task), logs it in `MessageLog`, NEW → AWAITING_CONFIRMATION; opted-out customers never contacted; failures keep the order NEW for retry.
- `GET/POST /webhook/meta`: verify-token handshake, `X-Hub-Signature-256` check, validated payloads, incoming messages stored once (dedupe by wamid) and linked to the right order (button payload only trusted for the sender's own orders), delivery statuses only move forward, failures recorded.
- Docs: `docs/meta-setup.md` (Meta app, numbers, tokens, webhook) and `docs/whatsapp-templates.md` (generated by `scripts/export_templates.py`, test keeps it in sync).
- Migration `0003` (message payload + customer link). 253 tests.

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
