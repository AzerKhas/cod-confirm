# Deploying COD Confirm

Three options, from the most control to the least work:

| Option | Cost (approx.) | HTTPS | Backups |
|---|---|---|---|
| **A. VPS + docker compose** (recommended) | 5–10 €/month (Hetzner CX22, OVH, Contabo…) | Caddy, automatic | `backup` service, nightly |
| B. Render | ~14 $/month (web + Postgres) | Render | Render Postgres backups |
| C. Railway | usage-based | Railway | Railway volume backups |

Run **one app instance**: the scheduler (reminders, quiet hours, daily summary) and the rate
limiter live in memory. One instance handles thousands of orders per day; to go further,
move the scheduler to its own container (`SCHEDULER_ENABLED=false` on the web instances) and
the rate limits to the proxy / Redis.

## A. VPS (Ubuntu 24.04) with docker compose

1. **Server**: create a VPS, add your SSH key, then:
   ```bash
   sudo apt update && sudo apt -y upgrade
   sudo apt -y install docker.io docker-compose-v2 ufw git
   sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
   sudo usermod -aG docker $USER   # log out / in
   ```
2. **DNS**: an `A` record `app.example.tn → <server IP>` (Caddy needs it to get the
   certificate).
3. **Code**: copy the project to the server (private git remote, or `scp -r`).
4. **Configuration**: `cp .env.example .env && chmod 600 .env`, then set at least:

   | Variable | Value |
   |---|---|
   | `APP_ENV` | `prod` |
   | `DOMAIN` / `ACME_EMAIL` | `app.example.tn` / your email (certificate expiry notices) |
   | `SECRET_KEY` | 48+ random chars (see SECURITY.md) |
   | `ENCRYPTION_KEY` | a Fernet key |
   | `POSTGRES_PASSWORD` | long random password |
   | `BACKUP_PASSPHRASE` | long random passphrase (keep a copy off the server!) |
   | `META_APP_SECRET`, `META_VERIFY_TOKEN` | from the Meta app (docs/meta-setup.md) |
   | `DRY_RUN` | `true` until the templates are approved, then `false` |
   | `TELEGRAM_BOT_TOKEN`, `OPERATOR_TELEGRAM_CHAT_ID` | alerts (owners + your error alerts) |
   | `GOOGLE_SERVICE_ACCOUNT_FILE` | optional, Sheets sync (mount the JSON read-only) |

5. **Start**:
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   docker compose -f docker-compose.prod.yml logs -f app      # JSON logs
   curl https://app.example.tn/health                         # {"status":"ok",...}
   ```
   Migrations run automatically at each start (`alembic upgrade head`).
6. **Operator account**:
   ```bash
   docker compose -f docker-compose.prod.yml exec app python -m scripts.create_user --email you@example.tn --role superadmin
   ```
   Then `https://app.example.tn/login` → `/admin` → create the first shop.
7. **Meta webhook**: callback `https://app.example.tn/webhook/meta`, verify token =
   `META_VERIFY_TOKEN`, field `messages`.

### Updates

```bash
git pull   # or copy the new version
docker compose -f docker-compose.prod.yml up -d --build app
```

The database schema is migrated on start. Read CHANGELOG.md before upgrading.

### Monitoring

- **Uptime**: an uptime monitor (UptimeRobot / Better Stack, free) on `https://<domain>/health`
  (returns 503 when the database is down).
- **Errors**: every ERROR log is sent to `OPERATOR_TELEGRAM_CHAT_ID` (max one message per
  minute, with a count). Optional Sentry: `pip install sentry-sdk` in the image and
  `SENTRY_DSN=...` (no personal data is sent).
- **Logs**: `docker compose -f docker-compose.prod.yml logs app` (JSON, rotated: 5 × 10 MB).
- **Disk**: `df -h` and the `backups/` folder size, monthly.

### Backups

`backups/` receives an encrypted `pg_dump` every night (14 days kept). **Copy it off the
server** every day, e.g. with `rclone` to an object storage bucket:
```bash
0 4 * * * rclone copy /home/deploy/cod-confirm/backups remote:cod-confirm-backups
```
Restore procedure: SECURITY.md → *Backups & restore*.

## B. Render

1. Push the project to a **private** GitHub/GitLab repository.
2. Render → *New* → *Blueprint* → select the repo (`render.yaml` creates the web service and
   the PostgreSQL database).
3. Fill the `sync: false` variables (`ENCRYPTION_KEY`, `ALLOWED_HOSTS=<your-service>.onrender.com`
   or your domain, Meta secrets, Telegram).
4. Shell tab → `python -m scripts.create_user --email you@example.tn --role superadmin`.

`DATABASE_URL` from Render (`postgres://…`) is converted automatically to the psycopg driver.

## C. Railway

1. New project → *Deploy from repo* (uses the Dockerfile) → add the **PostgreSQL** plugin.
2. Variables: `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `APP_ENV=prod`, `SECRET_KEY`,
   `ENCRYPTION_KEY`, `ALLOWED_HOSTS=<app>.up.railway.app`, `FORWARDED_ALLOW_IPS=*`, Meta and
   Telegram secrets.
3. Settings → Networking → *Generate domain*; health check path `/health`.

## Demo for prospects

On a staging instance (never in production — the script refuses `APP_ENV=prod`):

```bash
docker compose exec app python -m scripts.seed_demo --orders 120 --days 30
```

Creates *Boutique Yasmine* (clothing shop) with realistic fake orders, conversations in
Darija / Arabic / French and every status, plus the login `demo@codconfirm.tn` (password
printed once). Show: the dashboard KPIs, an order conversation, the CSV export, the public
form `/form/demo`.

## Environment variables (reference)

All variables are documented in [.env.example](.env.example). Production refuses to start
when `SECRET_KEY` is weak, `ENCRYPTION_KEY` is missing / invalid, `ALLOWED_HOSTS` is `*`, or
`META_APP_SECRET` is missing while `DRY_RUN=false`.
