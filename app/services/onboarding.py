"""Self-service sign-up and the "getting started" checklist for new sellers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import MIN_PASSWORD_LENGTH, hash_password
from app.enums import Language, Role
from app.models import Order, Payment, Shop, User
from app.plans import Plan
from app.security import generate_api_key
from app.services.billing import today_for, trial_end

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,24}$", re.IGNORECASE)


class SignupError(ValueError):
    """Invalid sign-up data; `field` tells the form where to show the message."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def slugify(name: str, max_len: int = 48) -> str:
    """'Boutique Yasmine & Co' -> 'boutique-yasmine-co' (ASCII; Arabic-only names -> '')."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:max_len].strip("-")


def unique_slug(db: Session, name: str) -> str:
    base = slugify(name) or "boutique"
    if len(base) < 3:
        base = f"boutique-{base}"
    slug, n = base, 2
    while db.scalar(select(Shop.id).where(Shop.slug == slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_account(
    db: Session,
    *,
    shop_name: str,
    email: str,
    password: str,
    language: Language = Language.DARIJA,
    terms_accepted: bool = False,
) -> tuple[Shop, User]:
    """New shop on the free trial + its owner account. Commits."""
    shop_name = " ".join(shop_name.split())
    email = email.strip().lower()
    if not 2 <= len(shop_name) <= 120:
        raise SignupError(
            "shop_name", "Le nom de la boutique doit faire entre 2 et 120 caractères."
        )
    if not EMAIL_RE.match(email):
        raise SignupError("email", "Adresse email invalide.")
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > 200:
        raise SignupError(
            "password", f"Le mot de passe doit faire au moins {MIN_PASSWORD_LENGTH} caractères."
        )
    if db.scalar(select(User.id).where(User.email == email)):
        # Same message whether the email exists or not would hide it, but the owner needs to
        # know they can log in instead: point to the login page without confirming more.
        raise SignupError("email", "Un compte existe déjà avec cet email. Connectez-vous.")

    _plain, key_hash, prefix = generate_api_key()  # the owner can generate a visible key later
    shop = Shop(
        name=shop_name,
        slug=unique_slug(db, shop_name),
        plan=Plan.TRIAL.value,
        default_language=language,
        api_key_hash=key_hash,
        api_key_prefix=prefix,
    )
    shop.paid_until = trial_end(today_for(shop))
    db.add(shop)
    db.flush()
    user = User(
        email=email,
        password_hash=hash_password(password),
        role=Role.OWNER,
        shop_id=shop.id,
        terms_accepted_at=datetime.now(UTC) if terms_accepted else None,
    )
    db.add(user)
    db.commit()
    return shop, user


# ------------------------------------------------------------------ checklist
@dataclass(frozen=True)
class Step:
    key: str
    title: str
    text: str
    done: bool
    optional: bool = False


def onboarding_steps(db: Session, shop: Shop) -> list[Step]:
    has_orders = bool(
        db.scalar(select(func.count()).select_from(Order).where(Order.shop_id == shop.id))
    )
    paid = bool(db.scalar(select(func.count()).select_from(Payment).where(
        Payment.shop_id == shop.id, Payment.status == "paid")))  # fmt: skip
    return [
        Step(
            "profile",
            "Présentez votre boutique",
            "Nom affiché aux clients, langue des messages et vos produits.",
            bool(shop.form_products),
        ),
        Step(
            "whatsapp",
            "Connectez votre numéro WhatsApp",
            "Le numéro qui enverra les confirmations à vos clients.",
            bool(shop.whatsapp_phone_number_id and shop.access_token_encrypted),
        ),
        Step(
            "orders",
            "Recevez vos commandes",
            "Formulaire pour Facebook / Instagram, Shopify, WooCommerce ou API.",
            has_orders or bool(shop.shopify_webhook_secret or shop.woocommerce_webhook_secret),
        ),
        Step(
            "test",
            "Faites une commande test",
            "Recevez vous-même le message WhatsApp et répondez « ey ».",
            has_orders,
        ),
        Step(
            "alerts",
            "Recevez les alertes sur Telegram",
            "Modifications demandées, questions et résumé du soir.",
            bool(shop.owner_telegram_chat_id),
            optional=True,
        ),
        Step(
            "plan",
            "Choisissez votre plan",
            "Essai gratuit en cours. Payez quand vous êtes convaincu.",
            paid,
            optional=True,
        ),
    ]


def progress(steps: list[Step]) -> tuple[int, int]:
    required = [s for s in steps if not s.optional]
    return sum(s.done for s in required), len(required)
