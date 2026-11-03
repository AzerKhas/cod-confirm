# Connecting WhatsApp (Meta Cloud API)

COD Confirm only uses the **official WhatsApp Business Platform (Cloud API)**.
No WhatsApp Web automation, no unofficial libraries.

## 1. One Meta app for the whole SaaS (done once, by the COD Confirm operator)

1. Create a **Meta Business portfolio** at <https://business.facebook.com> and verify the business
   (needed to go above the test limits and to become a Tech Provider).
2. Go to <https://developers.facebook.com/apps> → **Create app** → type **Business** →
   add the **WhatsApp** product.
3. **App settings → Basic**: copy the **App secret** → `META_APP_SECRET` in `.env`.
4. **WhatsApp → Configuration → Webhook**:
   - Callback URL: `https://<your-domain>/webhook/meta`
   - Verify token: any long random string → `META_VERIFY_TOKEN` in `.env`
   - Subscribe to the **messages** field.
5. To let shops connect their own number in one click later, apply to become a
   **Tech Provider** and enable **Embedded Signup** (Phase 8+). Until then, connect each
   shop manually as below.

## 2. Each shop (seller)

1. In **WhatsApp Manager**, add the seller's phone number (a number **not** used in the
   WhatsApp mobile app) and verify it by SMS / call.
2. Copy:
   - **Phone number ID** → `shops.whatsapp_phone_number_id`
   - **WhatsApp Business Account ID** → `shops.waba_id`
3. Create a **System User** (Business settings → Users → System users), give it the
   WhatsApp Business Account asset with *full control*, and generate a **permanent token**
   with the permissions `whatsapp_business_messaging` and `whatsapp_business_management`.
   The token is stored **encrypted** (`shops.access_token_encrypted`, Fernet with
   `ENCRYPTION_KEY`), never in plain text. The admin page (Phase 8) does this for you.
4. Submit the 9 templates of [whatsapp-templates.md](whatsapp-templates.md) in the shop's
   WhatsApp Business Account (or in a shared WABA). Approval usually takes minutes to hours.
5. Set `DRY_RUN=false` once templates are approved.

## 3. Test without Meta (DRY_RUN)

With `DRY_RUN=true` (default) every message is written to the JSON logs
(`whatsapp[dry-run]: message not sent`, phone masked) and gets a fake id `dryrun.<uuid>`.
The full flow (orders, statuses, dashboard) works without any Meta account.

## Rules enforced by the code

- Business-initiated messages = approved templates only.
- Free-form text / buttons only within 24 h after the customer's last message.
- `STOP`, `ma t3awedch`, `توقف`... → customer opted out, never contacted again.
- Webhook signature `X-Hub-Signature-256` checked on every call.
