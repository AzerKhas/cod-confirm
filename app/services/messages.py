"""User-facing texts in Tunisian Darija (Latin letters), Arabic and French.

Darija is written the way people really write on WhatsApp in Tunisia:
short, warm, everyday words, numbers for sounds (3 = ع, 7 = ح, 9 = ق, 5 = خ).

Phase 2: texts of the hosted order form. WhatsApp messages are added in Phases 3-4.
"""

from __future__ import annotations

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
