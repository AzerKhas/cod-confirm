"""Create a shop from the command line and print its intake API key (shown once).

    python -m scripts.create_shop --name "Skmei Tunisie" --slug skmei-tn \
        --product "Skmei 1251=59" --product "Skmei 1335=69"
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.enums import Language
from app.models import Shop
from app.security import generate_api_key


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--language", choices=[lang.value for lang in Language], default="darija")
    ap.add_argument("--product", action="append", default=[], help='"Name=price", repeatable')
    args = ap.parse_args()

    products = []
    for p in args.product:
        name, _, price = p.rpartition("=")
        amount = Decimal(price).quantize(Decimal("0.001"))
        products.append({"name": name.strip(), "price": str(amount)})

    with SessionLocal() as db:
        if db.scalar(select(Shop).where(Shop.slug == args.slug)):
            raise SystemExit(f"shop '{args.slug}' already exists")
        plain, key_hash, prefix = generate_api_key()
        db.add(
            Shop(
                name=args.name,
                slug=args.slug,
                default_language=Language(args.language),
                form_products=products,
                api_key_hash=key_hash,
                api_key_prefix=prefix,
                plan="trial",
                paid_until=date.today() + timedelta(days=get_settings().trial_days),
            )
        )
        db.commit()

    print(f"Shop created: /form/{args.slug}")
    print(f"API key (store it now, it is not shown again): {plain}")


if __name__ == "__main__":
    main()
