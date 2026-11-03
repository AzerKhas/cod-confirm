"""Create a dashboard account.

    python -m scripts.create_user --email owner@shop.tn --shop skmei-tn            # shop owner
    python -m scripts.create_user --email ops@codconfirm.tn --role superadmin      # operator

The password is asked interactively (or read from the CC_PASSWORD environment variable).
"""

from __future__ import annotations

import argparse
import getpass
import os

from sqlalchemy import select

from app.auth import MIN_PASSWORD_LENGTH, hash_password
from app.db import SessionLocal
from app.enums import Role
from app.models import Shop, User


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True)
    ap.add_argument("--shop", help="shop slug (not for superadmin)")
    ap.add_argument("--role", choices=[r.value for r in Role], default="owner")
    args = ap.parse_args()

    role = Role(args.role)
    password = os.environ.get("CC_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")

    with SessionLocal() as db:
        shop_id = None
        if role is not Role.SUPERADMIN:
            if not args.shop:
                raise SystemExit("--shop is required for owner / staff accounts")
            shop = db.scalar(select(Shop).where(Shop.slug == args.shop))
            if shop is None:
                raise SystemExit(f"unknown shop '{args.shop}'")
            shop_id = shop.id
        email = args.email.strip().lower()
        if db.scalar(select(User).where(User.email == email)):
            raise SystemExit("this email already has an account")
        db.add(User(email=email, password_hash=hash_password(password), role=role, shop_id=shop_id))
        db.commit()
    print(f"account created: {email} ({role.value})")


if __name__ == "__main__":
    main()
