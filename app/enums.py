"""Domain enums (stored as plain strings in the database)."""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    NEW = "NEW"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    MODIFY_REQUESTED = "MODIFY_REQUESTED"
    CANCELLED = "CANCELLED"
    NO_ANSWER = "NO_ANSWER"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    REFUSED = "REFUSED"


class OrderSource(StrEnum):
    FORM = "form"
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    API = "api"


class Actor(StrEnum):
    BOT = "bot"
    CUSTOMER = "customer"
    ADMIN = "admin"


class Language(StrEnum):
    DARIJA = "darija"
    ARABIC = "ar"
    FRENCH = "fr"


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


class MessageType(StrEnum):
    TEMPLATE = "template"
    TEXT = "text"
    BUTTON = "button"


class MessageStatus(StrEnum):
    RECEIVED = "received"  # incoming message
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
