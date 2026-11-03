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
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
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

    # Telegram chat id of the owner, for alerts (modification requests, questions).
    owner_telegram_chat_id: Mapped[str | None] = mapped_column(String(40))

    # Webhook signing secrets (encrypted at rest in Phase 9).
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
