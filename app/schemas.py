"""Pydantic v2 schemas (API input/output)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import Language, OrderStatus
from app.services.phone import InvalidPhone, normalize_phone

MILLIME = Decimal("0.001")
MAX_AMOUNT = Decimal("1000000")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MILLIME, rounding=ROUND_HALF_UP)


class OrderItemIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    qty: int = Field(ge=1, le=1000)
    price: Decimal = Field(ge=0, le=MAX_AMOUNT)

    @field_validator("price")
    @classmethod
    def _round_price(cls, v: Decimal) -> Decimal:
        return _money(v)

    def as_json(self) -> dict[str, object]:
        return {"name": self.name, "qty": self.qty, "price": str(self.price)}


class OrderIn(BaseModel):
    """A new order, whatever its source (API, Shopify, WooCommerce, form)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    external_id: str | None = Field(default=None, max_length=64)
    customer_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=4, max_length=32, description="Normalized to +216XXXXXXXX")
    address: str = Field(min_length=1, max_length=300)
    city: str = Field(min_length=1, max_length=80)
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    total: Decimal | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    currency: str = Field(default="TND", pattern=r"^[A-Z]{3}$")
    language: Language | None = None

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        try:
            return normalize_phone(v)
        except InvalidPhone as exc:
            raise ValueError(f"invalid phone number: {exc}") from exc

    @field_validator("total")
    @classmethod
    def _round_total(cls, v: Decimal | None) -> Decimal | None:
        return None if v is None else _money(v)

    def computed_total(self) -> Decimal:
        """Given total (may include shipping/discount) or sum of the items."""
        if self.total is not None:
            return self.total
        return _money(sum((i.price * i.qty for i in self.items), Decimal(0)))


class OrderCreatedOut(BaseModel):
    id: int
    status: OrderStatus
    created: bool = Field(description="false when this external_id was already imported")
    customer_id: int
    risk_warning: bool = Field(description="customer has refused orders before")


class WebhookResultOut(BaseModel):
    status: Literal["created", "duplicate", "ignored", "rejected", "ping"]
    order_id: int | None = None
    reason: str | None = None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    env: str
    db: Literal["ok", "error"]
    dry_run: bool
