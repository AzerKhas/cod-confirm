"""Re-encrypt every stored shop secret with the primary ENCRYPTION_KEY.

    python -m scripts.rotate_secrets

Run after putting a new key first in ENCRYPTION_KEY ("new,old"); also encrypts secrets
saved in plain text by older versions. Safe to run several times.
"""

from __future__ import annotations

from sqlalchemy import select

from app.crypto import rotate
from app.db import SessionLocal
from app.models import Shop

FIELDS = ("access_token_encrypted", "shopify_webhook_secret", "woocommerce_webhook_secret")


def main() -> None:
    count = 0
    with SessionLocal() as db:
        for shop in db.scalars(select(Shop)):
            for field in FIELDS:
                value = getattr(shop, field)
                if value:
                    setattr(shop, field, rotate(value))
                    count += 1
        db.commit()
    print(f"re-encrypted {count} secret(s)")


if __name__ == "__main__":
    main()
