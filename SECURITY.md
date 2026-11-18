# Security – COD Confirm

COD Confirm stores personal data of the sellers' customers (name, phone, address, orders)
and credentials of the sellers (WhatsApp tokens, webhook secrets). This document lists how
they are protected, how to operate the service safely, and how to report a problem.

## Reporting a vulnerability

Write to the operator privately (do not open a public issue). Include the steps to
reproduce. You get an answer within 72 hours; please give us 30 days to fix before any
disclosure.

## Data we keep (minimum needed)

| Data | Why | Where |
|---|---|---|
| Customer name, phone (E.164), address, city | deliver and confirm the order | `customers`, `orders` |
| Order items, total | confirm the order | `orders` |
| WhatsApp messages exchanged | history shown to the seller | `message_logs` |
| Opt-out flag + date | never contact again after STOP | `customers` |

- **Retention**: finished orders older than `shops.data_retention_days` (default 365) are
  deleted every night, with customers left without orders. Active orders are never purged.
- **Right to erasure**: "Delete this customer's data" in the order page (owner only), or
  `app.services.privacy.delete_customer` → cascades to orders, messages and events.
- **Logs never contain** full phone numbers (`+216*****456`), tokens, passwords or message
  content of free-text replies beyond what is needed for debugging.
- Usage counters (billing) keep only counts per month, no personal data.

In Tunisia, processing personal data falls under the *Loi organique n° 2004-63* (INPDP):
each seller remains the data controller for their customers; the operator processes data on
their behalf. Declare the processing to the INPDP if required and sign a data-processing
agreement with every seller.

## Controls in place

**Authentication & sessions**
- Passwords hashed with scrypt (n=2^14, r=8, p=1, random salt), min. 10 characters.
- Signed session cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in prod), renewed at login
  (no session fixation), revocable (`session_version` bumped on password reset).
- Login throttling: 5 failures / 15 min per (IP, email) + 20 attempts / min per IP;
  constant-time dummy hash so unknown emails can't be detected by timing.
- Roles: `owner`, `staff` (orders only), `superadmin` (operator). Every query is scoped to
  the user's shop: another shop's order or customer returns 404 (tested).

**Requests**
- CSRF token on every dashboard / admin POST (form field or `X-CSRF-Token` for HTMX).
- Public order form: signed, expiring form token (2 h), honeypot, 10 POST/min per IP,
  prices always taken from the shop catalogue (never from the browser).
- Intake API: per-shop API key (only its SHA-256 hash is stored, shown once), rate limit
  per plan. Shopify / WooCommerce: constant-time HMAC-SHA256 check of the raw body.
- Meta webhook: `X-Hub-Signature-256` checked on every call, payloads validated with
  Pydantic, duplicates ignored, button payloads only trusted for the sender's own orders.
- Pydantic validation with strict lengths on every input; body size limit (1 MB) on every
  endpoint, also for chunked uploads.
- `TrustedHostMiddleware` (`ALLOWED_HOSTS`, mandatory in prod).
- Jinja2 autoescape everywhere; CSV export and Google Sheets protected against formula
  injection (`'` prefix / RAW input).

**Browser hardening (every response)**
`Content-Security-Policy` (no inline script or style, everything self-hosted except HTMX
pinned with SRI, `frame-ancestors 'none'`), `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`,
`Cross-Origin-Opener-Policy`, `Strict-Transport-Security` in prod, `Cache-Control: no-store`
on private pages. API docs (`/docs`, `/openapi.json`) disabled in prod.

**Secrets**
- All secrets come from environment variables (`.env`, never committed; `.env.example`
  documents them). Settings refuse to start in prod with weak / missing secrets.
- WhatsApp tokens and webhook secrets encrypted at rest with Fernet (`ENCRYPTION_KEY`).
- Secrets are never shown back in the UI (empty field = keep the stored value).

**WhatsApp policy**
Official Cloud API only; business-initiated messages only with approved templates;
free-form replies only inside the 24 h window; STOP / `ma t3awedch` / `توقف` honoured
forever (until START); quiet hours 22:00–08:00.

## Operating safely

### Generate the secrets

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                                  # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"     # ENCRYPTION_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"                                  # META_VERIFY_TOKEN
```

Store them in the server's `.env` (mode `600`, owned by the deploy user) or in the host's
secret manager. Never in git, never in a chat.

### Rotate the encryption key

1. `ENCRYPTION_KEY=<new>,<old>` and restart (old data stays readable).
2. `docker compose exec app python -m scripts.rotate_secrets`
3. `ENCRYPTION_KEY=<new>` and restart.

Rotating `SECRET_KEY` logs every user out (sessions and form tokens are signed with it).

### Dependencies

```bash
pip install -r requirements-dev.txt
pip-audit -r requirements.txt          # known CVEs (0 at the last audit)
ruff check .                           # includes bandit-style security rules (S)
```

Run `pip-audit` before every release and monthly. Docker images install the pinned
`requirements.lock`.

### Backups & restore

- `deploy/backup.sh` runs every night in the `backup` service of the production compose
  file: `pg_dump` custom format, optional AES-256 encryption (`BACKUP_PASSPHRASE`),
  14 days kept. **Copy the backup folder off the server** (another provider / S3 bucket).
- Restore test (do it once a month on a staging machine):

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in cod_confirm-XXXX.dump.enc -out restore.dump -pass env:BACKUP_PASSPHRASE
pg_restore --clean --no-owner -d cod_confirm restore.dump
```

### Incident response (short)

1. Suspend affected shops (`/admin` → uncheck *Active*) — nothing is sent anymore.
2. Rotate exposed secrets: Meta system-user token (Business settings), webhook secrets,
   `ENCRYPTION_KEY` / `SECRET_KEY`, database password.
3. Reset passwords of affected accounts (`/admin` → *Reset password*, logs out everywhere).
4. Check JSON logs (`docker compose logs app`) for the time window; restore from backup if
   data was altered.
5. Inform the affected sellers (data controllers) within 72 h; they inform their customers
   and the INPDP when required.

## Penetration-test checklist

Run before going live and after major changes (on staging, never on production data):

- [ ] Login: brute force blocked (429), no user enumeration (same error, same timing).
- [ ] Session: cookie `HttpOnly` + `Secure` + `SameSite`; logout invalidates; password reset
      kills other sessions.
- [ ] IDOR: with owner A's session, request orders / customers / CSV of shop B by id → 404.
- [ ] Privilege: staff cannot open settings or delete customers; owner cannot open `/admin`.
- [ ] CSRF: every POST without / with a wrong token → 403.
- [ ] XSS: customer name / address / WhatsApp message `<script>alert(1)</script>` and
      `"><img src=x onerror=alert(1)>` rendered as text in dashboard, form and admin.
- [ ] CSV / Sheets injection: name `=HYPERLINK("http://evil")` exported as text.
- [ ] Webhooks: Meta / Shopify / WooCommerce with missing, wrong or replayed-but-modified
      signature → 401; huge body → 413; malformed JSON → no 500.
- [ ] Intake API: wrong key → 401; other shop's key cannot touch this shop; rate limit → 429.
- [ ] Public form: no token / expired token → 400; price tampering ignored; honeypot.
- [ ] Opt-out: STOP then a new order → no template sent.
- [ ] Host header: unknown `Host` → 400.
- [ ] Headers: CSP, HSTS, X-Frame-Options present (e.g. securityheaders.com).
- [ ] TLS: A grade on ssllabs.com; HTTP redirects to HTTPS.
- [ ] Secrets: no token in logs, HTML, error pages or git history (`git log -p | grep -i EAAG`).
- [ ] Database: not exposed publicly (only on the internal Docker network).
- [ ] Backups: restore test succeeded this month.
- [ ] `pip-audit` clean.

Most of these checks are automated in `tests/` (`test_security.py`, `test_dashboard.py`,
`test_admin_tenancy.py`, `test_webhook_meta.py`, `test_intake_webhooks.py`).
