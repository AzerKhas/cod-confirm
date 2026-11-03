"""User-facing texts in Tunisian Darija (Latin letters), Arabic and French.

Darija is written the way people really write on WhatsApp in Tunisia:
short, warm, everyday words, numbers for sounds (3 = ع, 7 = ح, 9 = ق, 5 = خ).

Contents: hosted order form texts, WhatsApp templates (Phase 3).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.enums import Language

FORM_TEXTS: dict[Language, dict[str, str]] = {
    Language.DARIJA: {
        "title": "Commandi taw",
        "subtitle": "Lekhlas ki toussel l commande 🚚",
        "name": "Esmek w la9bek",
        "phone": "Noumrou mte3ek (WhatsApp)",
        "phone_hint": "8 ar9am, mathalan 98 123 456",
        "city": "Wileya",
        "city_placeholder": "A5tar l wileya",
        "address": "L'adresse kemla",
        "address_hint": "Nahj, nounrou, 9rib men chnowa…",
        "product": "Chnowa t7eb ?",
        "qty": "9addech",
        "submit": "Akked l commande",
        "done_title": "Ya3tik essa7a ! ✅",
        "done_body": "Commande mte3ek wselet. Taw nab3thoulek message 3al WhatsApp bech tconfirmi.",
        "err_required": "Lazem t3abbi hedhi",
        "err_phone": "Noumrou mouch s7i7. Ekteb 8 ar9am, mathalan 98 123 456",
        "err_product": "A5tar produit",
        "err_generic": "Fama ghalta, thabbet fel ma3loumet w 3awed",
    },
    Language.ARABIC: {
        "title": "اطلب الآن",
        "subtitle": "الدفع عند الاستلام 🚚",
        "name": "الاسم واللقب",
        "phone": "رقم الهاتف (واتساب)",
        "phone_hint": "8 أرقام، مثلا 98 123 456",
        "city": "الولاية",
        "city_placeholder": "اختر الولاية",
        "address": "العنوان الكامل",
        "address_hint": "النهج، الرقم، قرب…",
        "product": "المنتج",
        "qty": "الكمية",
        "submit": "تأكيد الطلب",
        "done_title": "شكرا لك ! ✅",
        "done_body": "وصلنا طلبك. سنرسل لك رسالة على واتساب لتأكيده.",
        "err_required": "هذا الحقل إجباري",
        "err_phone": "رقم الهاتف غير صحيح. اكتب 8 أرقام، مثلا 98123456",
        "err_product": "اختر المنتج",
        "err_generic": "هناك خطأ، تثبت من المعطيات وأعد المحاولة",
    },
    Language.FRENCH: {
        "title": "Commander maintenant",
        "subtitle": "Paiement à la livraison 🚚",
        "name": "Nom et prénom",
        "phone": "Téléphone (WhatsApp)",
        "phone_hint": "8 chiffres, ex. 98 123 456",
        "city": "Gouvernorat",
        "city_placeholder": "Choisir le gouvernorat",
        "address": "Adresse complète",
        "address_hint": "Rue, numéro, à côté de…",
        "product": "Produit",
        "qty": "Quantité",
        "submit": "Valider la commande",
        "done_title": "Merci ! ✅",
        "done_body": "Votre commande est bien reçue. Nous vous envoyons un message WhatsApp "
        "pour la confirmer.",
        "err_required": "Champ obligatoire",
        "err_phone": "Numéro invalide. Saisissez 8 chiffres, ex. 98 123 456",
        "err_product": "Choisissez un produit",
        "err_generic": "Une erreur est survenue, vérifiez les informations",
    },
}

# The 24 governorates of Tunisia.
TN_GOVERNORATES: tuple[str, ...] = (
    "Ariana", "Béja", "Ben Arous", "Bizerte", "Gabès", "Gafsa", "Jendouba", "Kairouan",
    "Kasserine", "Kébili", "Le Kef", "Mahdia", "La Manouba", "Médenine", "Monastir",
    "Nabeul", "Sfax", "Sidi Bouzid", "Siliana", "Sousse", "Tataouine", "Tozeur", "Tunis",
    "Zaghouan",
)  # fmt: skip


def form_texts(language: Language) -> dict[str, str]:
    return FORM_TEXTS.get(language, FORM_TEXTS[Language.FRENCH])


# =============================================================================
# WhatsApp message templates (business-initiated, must be approved by Meta)
# =============================================================================
# Generic for every kind of shop (clothes, cosmetics, watches, shoes...).
# Body rules from Meta: variables {{1}}..{{n}}, the body must not start or end with
# a variable, no two variables side by side, parameters without new lines.
# Quick-reply buttons: max 3, max 25 characters each.


@dataclass(frozen=True)
class WaTemplate:
    key: str
    category: str
    variables: tuple[str, ...]  # what each {{n}} contains (docs)
    samples: tuple[str, ...]  # example values sent to Meta for review
    bodies: dict[Language, str]
    footers: dict[Language, str] = field(default_factory=dict)
    buttons: dict[Language, tuple[str, ...]] = field(default_factory=dict)

    def name(self, language: Language) -> str:
        return f"cod_{self.key}_{TEMPLATE_SUFFIX[language]}"

    def render(self, language: Language, params: Sequence[str]) -> str:
        """Local preview of the message (stored in MessageLog / shown in the dashboard)."""
        text = self.bodies[language]
        for i, value in enumerate(params, start=1):
            text = text.replace("{{" + str(i) + "}}", value)
        return text


TEMPLATE_SUFFIX: dict[Language, str] = {
    Language.DARIJA: "tn",
    Language.ARABIC: "ar",
    Language.FRENCH: "fr",
}

_STOP_FOOTER = {
    Language.DARIJA: "Ekteb STOP ken ma t7ebch twasslek messaget",
    Language.ARABIC: "أرسل STOP أو توقف لإيقاف الرسائل",
    Language.FRENCH: "Répondez STOP pour ne plus recevoir de messages",
}

CONFIRM_BUTTONS: dict[Language, tuple[str, ...]] = {
    Language.DARIJA: ("✅ Nconfirmi", "✏️ Nbaddel haja", "❌ Nannuli"),
    Language.ARABIC: ("✅ تأكيد", "✏️ تعديل", "❌ إلغاء"),
    Language.FRENCH: ("✅ Confirmer", "✏️ Modifier", "❌ Annuler"),
}

# Button order = payload order: index 0 confirm, 1 modify, 2 cancel.
BUTTON_ACTIONS: tuple[str, ...] = ("CONFIRM", "MODIFY", "CANCEL")

ORDER_CONFIRMATION = WaTemplate(
    key="order_confirmation",
    category="UTILITY",
    variables=("customer first name", "shop name", "items summary", "total", "city"),
    samples=("Ahmed", "Boutique Yasmine", "1× Robe d'été bleue, 2× Foulard", "89.500", "Sousse"),
    bodies={
        Language.DARIJA: (
            "Salam {{1}} 👋\n"
            "Ya3tik essa7a 3al commande mte3ek men {{2}} !\n\n"
            "🛍️ {{3}}\n"
            "💰 {{4}} DT, tkhallas ki tousel\n"
            "📍 Livraison l {{5}}\n\n"
            "Tconfirmi l commande ?"
        ),
        Language.ARABIC: (
            "مرحبا {{1}} 👋\n"
            "شكرا على طلبك من {{2}} !\n\n"
            "🛍️ {{3}}\n"
            "💰 {{4}} د.ت، الدفع عند الاستلام\n"
            "📍 التوصيل إلى {{5}}\n\n"
            "هل تؤكد الطلب؟"
        ),
        Language.FRENCH: (
            "Bonjour {{1}} 👋\n"
            "Merci pour votre commande chez {{2}} !\n\n"
            "🛍️ {{3}}\n"
            "💰 {{4}} DT, paiement à la livraison\n"
            "📍 Livraison à {{5}}\n\n"
            "Confirmez-vous la commande ?"
        ),
    },
    footers=_STOP_FOOTER,
    buttons=CONFIRM_BUTTONS,
)

ORDER_REMINDER = WaTemplate(
    key="order_reminder",
    category="UTILITY",
    variables=("customer first name", "shop name", "total"),
    samples=("Ahmed", "Boutique Yasmine", "89.500"),
    bodies={
        Language.DARIJA: (
            "Salam {{1}} 🙏\n"
            "Mazelna nestannew jawbek 3al commande mte3ek men {{2}} ({{3}} DT).\n"
            "Tconfirmi bech nab3thouhalek ?"
        ),
        Language.ARABIC: (
            "مرحبا {{1}} 🙏\n"
            "مازلنا ننتظر ردك على طلبك من {{2}} ({{3}} د.ت).\n"
            "هل تؤكد الطلب حتى نرسله لك؟"
        ),
        Language.FRENCH: (
            "Bonjour {{1}} 🙏\n"
            "Nous attendons toujours votre réponse pour votre commande chez {{2}} ({{3}} DT).\n"
            "Confirmez-vous l'envoi ?"
        ),
    },
    footers=_STOP_FOOTER,
    buttons=CONFIRM_BUTTONS,
)

ORDER_SHIPPED = WaTemplate(
    key="order_shipped",
    category="UTILITY",
    variables=("customer first name", "shop name", "total"),
    samples=("Ahmed", "Boutique Yasmine", "89.500"),
    bodies={
        Language.DARIJA: (
            "Ya {{1}}, commande mte3ek men {{2}} 5arjet m3a el livreur 🚚\n"
            "Bech y3ayetlek 9rib. 7adher {{3}} DT ki tousel.\n"
            "Ya3tik essa7a !"
        ),
        Language.ARABIC: (
            "مرحبا {{1}}، طلبك من {{2}} خرج مع عون التوصيل 🚚\n"
            "سيتصل بك قريبا. حضّر {{3}} د.ت عند الاستلام.\n"
            "شكرا لك !"
        ),
        Language.FRENCH: (
            "Bonjour {{1}}, votre commande chez {{2}} est en route avec le livreur 🚚\n"
            "Il vous appellera bientôt. Prévoyez {{3}} DT à la livraison.\n"
            "Merci !"
        ),
    },
    footers=_STOP_FOOTER,
)

TEMPLATES: tuple[WaTemplate, ...] = (ORDER_CONFIRMATION, ORDER_REMINDER, ORDER_SHIPPED)


# ----------------------------------------------------------- parameter helpers
_WS = re.compile(r"\s+")


def clean_param(value: str, max_len: int = 120) -> str:
    """Meta rejects parameters with new lines, tabs or 4+ consecutive spaces."""
    text = _WS.sub(" ", value).strip() or "-"
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def first_name(full_name: str) -> str:
    return clean_param(full_name.split()[0] if full_name.split() else full_name, 40)


def format_amount(amount: Decimal) -> str:
    """118.000 -> '118', 89.5 -> '89.500' (Tunisian dinar has 3 decimals)."""
    q = amount.quantize(Decimal("0.001"))
    return str(q.to_integral_value()) if q == q.to_integral_value() else f"{q:.3f}"


def items_summary(items: Sequence[dict[str, Any]], max_len: int = 120) -> str:
    parts = [f"{int(i.get('qty', 1))}× {i.get('name', '')}" for i in items]
    return clean_param(", ".join(parts), max_len)
