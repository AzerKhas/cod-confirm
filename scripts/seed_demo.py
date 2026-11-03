"""Fill the database with a demo shop and fake orders (for sales demos / local dev).

    python -m scripts.seed_demo                       # demo shop "Boutique Yasmine"
    python -m scripts.seed_demo --orders 120 --days 30

Creates the shop `demo`, an owner account demo@codconfirm.tn (password printed once, or
CC_PASSWORD), and orders spread over the last days in every status with realistic
WhatsApp conversations in Darija / Arabic / French. All names and numbers are fake.
Refuses to run in production.
"""

from __future__ import annotations

import argparse
import os
import random
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.enums import Actor, Direction, Language, MessageStatus, MessageType, OrderSource, Role
from app.enums import OrderStatus as S
from app.models import Customer, Event, MessageLog, Order, Shop, User
from app.security import generate_api_key
from app.services import messages as m

FIRST = ["Ahmed", "Mariem", "Yassine", "Sana", "Karim", "Ines", "Mehdi", "Rania", "Aymen",
         "Nour", "Hamza", "Salma", "Wael", "Emna", "Bilel", "Asma", "Seif", "Amira"]  # fmt: skip
LAST = ["Ben Salah", "Trabelsi", "Gharbi", "Jaziri", "Mejri", "Ayari", "Hammami", "Sassi",
        "Chaabane", "Dridi", "Khelifi", "Mansouri"]  # fmt: skip
CITIES = ["Tunis", "Sfax", "Sousse", "Nabeul", "Bizerte", "Monastir", "Ariana", "Gabès",
          "Kairouan", "Ben Arous", "Mahdia", "Médenine"]  # fmt: skip
PRODUCTS = [("Robe d'été fleurie", "69.000"), ("Jean slim", "55.000"), ("Foulard en soie", "25.000"),
            ("Chemise lin", "49.000"), ("Abaya brodée", "120.000"), ("Basket blanche", "89.000")]  # fmt: skip
REPLIES = {
    S.CONFIRMED: ["ey nconfirmi", "Oui je confirme", "نعم", "behi ✅", "mrigel"],
    S.CANCELLED: ["la ma n7ebch", "Non merci", "لا", "nannuli"],
    S.MODIFY_REQUESTED: ["n7eb nbaddel l 9yas L", "je veux changer la couleur", "نحب نبدل العنوان"],
}
WEIGHTS = [(S.DELIVERED, 30), (S.CONFIRMED, 14), (S.SHIPPED, 12), (S.CANCELLED, 10),
           (S.NO_ANSWER, 8), (S.AWAITING_CONFIRMATION, 12), (S.MODIFY_REQUESTED, 5),
           (S.REFUSED, 5), (S.NEW, 4)]  # fmt: skip
PATHS = {
    S.NEW: [],
    S.AWAITING_CONFIRMATION: [S.AWAITING_CONFIRMATION],
    S.CONFIRMED: [S.AWAITING_CONFIRMATION, S.CONFIRMED],
    S.CANCELLED: [S.AWAITING_CONFIRMATION, S.CANCELLED],
    S.MODIFY_REQUESTED: [S.AWAITING_CONFIRMATION, S.MODIFY_REQUESTED],
    S.NO_ANSWER: [S.AWAITING_CONFIRMATION, S.NO_ANSWER],
    S.SHIPPED: [S.AWAITING_CONFIRMATION, S.CONFIRMED, S.SHIPPED],
    S.DELIVERED: [S.AWAITING_CONFIRMATION, S.CONFIRMED, S.SHIPPED, S.DELIVERED],
    S.REFUSED: [S.AWAITING_CONFIRMATION, S.CONFIRMED, S.SHIPPED, S.REFUSED],
}
ACTORS = {S.AWAITING_CONFIRMATION: Actor.BOT, S.NO_ANSWER: Actor.BOT, S.CONFIRMED: Actor.CUSTOMER,
          S.CANCELLED: Actor.CUSTOMER, S.MODIFY_REQUESTED: Actor.CUSTOMER}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orders", type=int, default=80)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if get_settings().app_env == "prod":
        raise SystemExit("refusing to seed demo data in production")
    rnd = random.Random(args.seed)
    password = os.environ.get("CC_PASSWORD") or secrets.token_urlsafe(12)

    with SessionLocal() as db:
        if db.scalar(select(Shop).where(Shop.slug == "demo")):
            raise SystemExit("demo shop already exists")
        _plain, key_hash, prefix = generate_api_key()
        shop = Shop(name="Boutique Yasmine", slug="demo", default_language=Language.DARIJA,
                    delivery_fee=Decimal("7.000"), api_key_hash=key_hash, api_key_prefix=prefix,
                    form_products=[{"name": n, "price": p} for n, p in PRODUCTS])  # fmt: skip
        db.add(shop)
        db.add(User(email="demo@codconfirm.tn", password_hash=hash_password(password),
                    role=Role.OWNER, shop=shop))  # fmt: skip
        customers: list[Customer] = []
        for i in range(max(10, args.orders * 2 // 3)):
            customers.append(Customer(
                shop=shop, name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
                phone_e164=f"+216{rnd.choice('2459')}{i:07d}",
                language=rnd.choice([None, None, Language.ARABIC, Language.FRENCH]),
                total_orders=0, total_refused=0, risk_score=0,
            ))  # fmt: skip
        db.add_all(customers)
        db.flush()

        statuses, weights = zip(*WEIGHTS, strict=True)
        now = datetime.now(UTC)
        for _ in range(args.orders):
            final = rnd.choices(statuses, weights)[0]
            if final in (S.DELIVERED, S.REFUSED):
                created = now - timedelta(days=rnd.uniform(2, args.days), hours=rnd.uniform(0, 12))
            else:
                created = now - timedelta(hours=rnd.uniform(0.5, min(args.days * 24, 72)))
            customer = rnd.choice(customers)
            name, price = rnd.choice(PRODUCTS)
            qty = rnd.choice([1, 1, 1, 2])
            order = Order(shop=shop, customer=customer, items=[{"name": name, "qty": qty, "price": price}],
                          total=Decimal(price) * qty, address=f"Rue {rnd.randint(1, 99)}, cité {rnd.choice(LAST)}",
                          city=rnd.choice(CITIES), source=rnd.choice(list(OrderSource)),
                          status=final, created_at=created, updated_at=created,
                          reminders_sent=2 if final is S.NO_ANSWER else 0)  # fmt: skip
            db.add(order)
            customer.total_orders += 1
            if final is S.REFUSED:
                customer.total_refused += 1
                customer.risk_score = 100 if customer.total_orders == 1 else 50
            lang = customer.language or shop.default_language
            t = created
            db.add(
                Event(order=order, from_status=None, to_status=S.NEW, actor=Actor.BOT, created_at=t)
            )
            prev = S.NEW
            for step in PATHS[final]:
                t += timedelta(minutes=rnd.randint(1, 180))
                if step is S.AWAITING_CONFIRMATION:
                    params = [m.first_name(customer.name), shop.name, m.items_summary(order.items),
                              m.format_amount(order.total), order.city]  # fmt: skip
                    db.add(MessageLog(order=order, customer_id=customer.id, direction=Direction.OUT,
                                      type=MessageType.TEMPLATE, status=MessageStatus.READ,
                                      content=m.ORDER_CONFIRMATION.render(lang, params), created_at=t))  # fmt: skip
                if step in REPLIES:
                    db.add(MessageLog(order=order, customer_id=customer.id, direction=Direction.IN, type=MessageType.TEXT,
                                      status=MessageStatus.RECEIVED, content=rnd.choice(REPLIES[step]),
                                      created_at=t))  # fmt: skip
                db.add(Event(order=order, from_status=prev, to_status=step,
                             actor=ACTORS.get(step, Actor.ADMIN), created_at=t))  # fmt: skip
                prev = step
        db.commit()
    print("Demo shop created: Boutique Yasmine (/form/demo)")
    print("Login: demo@codconfirm.tn")
    if not os.environ.get("CC_PASSWORD"):
        print(f"Password (shown once): {password}")


if __name__ == "__main__":
    main()
