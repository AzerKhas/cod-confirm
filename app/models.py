"""ORM models: Shop, Customer, Order, MessageLog, Event.

Personal data kept to the minimum needed: customer name, phone, address and the order.
Deleting a Customer cascades to their orders, messages and events (see `services/privacy.py`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    Actor,
    Direction,
    Language,
    MessageStatus,
    MessageType,
    OrderSource,
    OrderStatus,
    Role,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Always store UTC, always return timezone-aware datetimes (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed, use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _enum(enum_cls: type) -> SAEnum:
    """Portable enum column: VARCHAR + CHECK, values stored (not names)."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        values_callable=lambda e: [m.value for m in e],
        length=32,
        validate_strings=True,
    )


Money = Numeric(12, 3)  # TND uses millimes (3 decimals)


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)

    # WhatsApp Cloud API
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(String(40))
    waba_id: Mapped[str | None] = mapped_column(String(40))
    # Encrypted at rest (Fernet, Phase 9). Never store the plain token.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)

    default_language: Mapped[Language] = mapped_column(_enum(Language), default=Language.DARIJA)
    timezone: Mapped[str] = mapped_column(String(50), default="Africa/Tunis")
    # Hours after the first message: [first reminder, second reminder].
    reminder_delays: Mapped[list[int]] = mapped_column(JSON, default=lambda: [3, 24])
    delivery_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("7.000"))
    data_retention_days: Mapped[int] = mapped_column(Integer, default=365)
    # Local date of the last daily summary / maintenance (avoids sending it twice).
    last_daily_run_on: Mapped[date | None] = mapped_column(Date)

    # Subscription (Phase 8). An inactive shop keeps its data but nothing is sent.
    plan: Mapped[str] = mapped_column(String(20), default="trial", server_default="trial")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    paid_until: Mapped[date | None] = mapped_column(Date)
    # Last local date a billing reminder / suspension notice was sent (once a day max).
    billing_notice_on: Mapped[date | None] = mapped_column(Date)

    # Google Sheets sync (optional): id of the sheet shared with the service account.
    google_sheet_id: Mapped[str | None] = mapped_column(String(100))

    # Public order form branding: accent colour (#rrggbb) and logo (stored in the DB because
    # the production container is read-only; deferred so it is only loaded when served).
    brand_color: Mapped[str | None] = mapped_column(String(7))
    logo: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    logo_mime: Mapped[str | None] = mapped_column(String(20))
    logo_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Delivery settings: default carrier, public tracking URL pattern ("...{tracking}..."),
    # and the secret that signs status updates pushed to /webhooks/carrier/<slug>.
    default_carrier: Mapped[str | None] = mapped_column(String(30))
    tracking_url_pattern: Mapped[str | None] = mapped_column(String(300))
    carrier_webhook_secret: Mapped[str | None] = mapped_column(Text)

    # Meta Pixel + Conversions API (optional). The browser pixel only loads after the customer
    # accepts the consent banner; the server sends a Purchase only for consented orders that
    # reach `meta_event_on` ("confirmed" or "delivered"). The CAPI token is encrypted.
    meta_pixel_id: Mapped[str | None] = mapped_column(String(20))
    meta_capi_token: Mapped[str | None] = mapped_column(Text)
    meta_test_event_code: Mapped[str | None] = mapped_column(String(40))
    meta_event_on: Mapped[str] = mapped_column(
        String(12), default="confirmed", server_default="confirmed"
    )

    # Telegram chat id of the owner, for alerts (modification requests, questions).
    owner_telegram_chat_id: Mapped[str | None] = mapped_column(String(40))

    # Webhook signing secrets (encrypted at rest, see app/crypto.py).
    shopify_webhook_secret: Mapped[str | None] = mapped_column(Text)
    woocommerce_webhook_secret: Mapped[str | None] = mapped_column(Text)
    # Products shown in the hosted form: [{"name": str, "price": "59.000"}]
    form_products: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )

    # Only a SHA-256 hash of the intake API key is stored; prefix shown in the UI.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    api_key_prefix: Mapped[str] = mapped_column(String(12))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    customers: Mapped[list[Customer]] = relationship(
        back_populates="shop", cascade="all, delete-orphan", passive_deletes=True
    )
    orders: Mapped[list[Order]] = relationship(back_populates="shop", passive_deletes=True)

    def __repr__(self) -> str:  # no secrets in repr
        return f"<Shop id={self.id} slug={self.slug!r}>"


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("shop_id", "phone_e164", name="uq_customer_shop_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone_e164: Mapped[str] = mapped_column(String(20))
    language: Mapped[Language | None] = mapped_column(_enum(Language))
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)  # 0..100
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_refused: Mapped[int] = mapped_column(Integer, default=0)
    # Start of the 24h customer-service window = last incoming message.
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    shop: Mapped[Shop] = relationship(back_populates="customers")
    orders: Mapped[list[Order]] = relationship(
        back_populates="customer", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # phone masked
        from app.security import mask_phone

        return f"<Customer id={self.id} phone={mask_phone(self.phone_e164)}>"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        # Idempotency: the same Shopify/Woo order is never imported twice.
        UniqueConstraint("shop_id", "source", "external_id", name="uq_order_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(64))
    # [{"name": str, "qty": int, "price": "str decimal"}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    total: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="TND")
    address: Mapped[str] = mapped_column(String(300))
    city: Mapped[str] = mapped_column(String(80))
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus), default=OrderStatus.NEW, index=True
    )
    source: Mapped[OrderSource] = mapped_column(_enum(OrderSource))
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    # True when the Google Sheet row must be (re)written.
    sheet_dirty: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Delivery: carrier code (services/shipping.CARRIERS) and its tracking number.
    carrier: Mapped[str | None] = mapped_column(String(30))
    tracking_number: Mapped[str | None] = mapped_column(String(64), index=True)
    shipped_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When the "order shipped" WhatsApp notice went out (None = still to send).
    shipped_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Ad measurement, only with the customer's consent on the form: browser ids (_fbp/_fbc),
    # IP, user agent and page URL. capi_state: None = to send, "sent", "error", "skipped".
    ad_consent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    ad_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    capi_state: Mapped[str | None] = mapped_column(String(10))
    capi_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_contacted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    shop: Mapped[Shop] = relationship(back_populates="orders")
    customer: Mapped[Customer] = relationship(back_populates="orders")
    messages: Mapped[list[MessageLog]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MessageLog.id",
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Event.id",
    )

    def __repr__(self) -> str:
        return f"<Order id={self.id} status={self.status}>"


class MessageLog(Base):
    __tablename__ = "message_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[Direction] = mapped_column(_enum(Direction))
    type: Mapped[MessageType] = mapped_column(_enum(MessageType))
    content: Mapped[str] = mapped_column(Text, default="")
    # Button payload (e.g. "CONFIRM:42") for incoming button replies.
    payload: Mapped[str | None] = mapped_column(String(256))
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[MessageStatus] = mapped_column(
        _enum(MessageStatus), default=MessageStatus.QUEUED
    )
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    order: Mapped[Order | None] = relationship(back_populates="messages")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[OrderStatus | None] = mapped_column(_enum(OrderStatus))
    to_status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus))
    actor: Mapped[Actor] = mapped_column(_enum(Actor))
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    order: Mapped[Order] = relationship(back_populates="events")


class User(Base):
    """Dashboard account. Passwords are stored as scrypt hashes only."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.OWNER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on password change / logout-everywhere: invalidates existing sessions.
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When the terms of service were accepted at sign-up (None: account created by the operator).
    terms_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    shop: Mapped[Shop | None] = relationship()

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role}>"


class UsageCounter(Base):
    """Monthly usage per shop (billing / quotas). Kept even when orders are purged."""

    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("shop_id", "month", name="uq_usage_shop_month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    month: Mapped[str] = mapped_column(String(7))  # "2026-11" (UTC)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    messages_sent: Mapped[int] = mapped_column(Integer, default=0)


class Payment(Base):
    """A subscription payment (Konnect, Flouci, manual transfer / cash, or demo)."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # konnect | flouci | manual | demo
    plan: Mapped[str] = mapped_column(String(20))
    months: Mapped[int] = mapped_column(Integer)
    amount_millimes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    # Provider reference (Konnect paymentRef / Flouci payment_id): unique per provider.
    provider_ref: Mapped[str | None] = mapped_column(String(100))
    pay_url: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(String(300))
    invoice_number: Mapped[str | None] = mapped_column(String(30), unique=True)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    checks: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("provider", "provider_ref", name="uq_payment_provider_ref"),)

    shop: Mapped[Shop] = relationship()
