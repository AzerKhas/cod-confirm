"""Order state machine.

    NEW -> AWAITING_CONFIRMATION -> CONFIRMED -> SHIPPED -> DELIVERED
                                 -> MODIFY_REQUESTED -> (admin edits) -> AWAITING_CONFIRMATION
                                 -> CANCELLED
                                 -> NO_ANSWER (after last reminder)
    SHIPPED -> REFUSED (returned)

Extra edges kept on purpose (real-life cases):
  * NEW -> CANCELLED              admin cancels before any message (fake order)
  * CONFIRMED -> CANCELLED        customer calls the shop to cancel before shipping
  * MODIFY_REQUESTED -> CANCELLED customer finally cancels
  * NO_ANSWER -> CONFIRMED / CANCELLED / AWAITING_CONFIRMATION
                                  late reply, or admin re-sends the confirmation

Every transition is validated (graph + who is allowed) and logged as an `Event`.
The caller owns the transaction: functions here `flush()` but never `commit()`.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.enums import Actor, OrderStatus
from app.models import Customer, Event, Order

log = logging.getLogger(__name__)
S = OrderStatus

TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.NEW: frozenset({S.AWAITING_CONFIRMATION, S.CANCELLED}),
    S.AWAITING_CONFIRMATION: frozenset({S.CONFIRMED, S.MODIFY_REQUESTED, S.CANCELLED, S.NO_ANSWER}),
    S.MODIFY_REQUESTED: frozenset({S.AWAITING_CONFIRMATION, S.CANCELLED}),
    S.NO_ANSWER: frozenset({S.CONFIRMED, S.CANCELLED, S.AWAITING_CONFIRMATION}),
    S.CONFIRMED: frozenset({S.SHIPPED, S.CANCELLED}),
    S.SHIPPED: frozenset({S.DELIVERED, S.REFUSED}),
    S.DELIVERED: frozenset(),
    S.REFUSED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL: frozenset[OrderStatus] = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

# Which target statuses each actor may set. Admin may do any valid transition.
ACTOR_TARGETS: dict[Actor, frozenset[OrderStatus]] = {
    Actor.BOT: frozenset({S.AWAITING_CONFIRMATION, S.NO_ANSWER}),
    Actor.CUSTOMER: frozenset({S.CONFIRMED, S.MODIFY_REQUESTED, S.CANCELLED}),
    Actor.ADMIN: frozenset(OrderStatus),
}

# Risk score: a customer is flagged (warning badge), never auto-blocked.
RISKY_THRESHOLD = 50


class InvalidTransition(ValueError):
    """Raised when a status change is not allowed."""

    def __init__(self, from_status: OrderStatus, to_status: OrderStatus, actor: Actor) -> None:
        super().__init__(f"{actor.value} cannot move order from {from_status} to {to_status}")
        self.from_status = from_status
        self.to_status = to_status
        self.actor = actor


def can_transition(
    from_status: OrderStatus, to_status: OrderStatus, actor: Actor = Actor.ADMIN
) -> bool:
    return to_status in TRANSITIONS[from_status] and to_status in ACTOR_TARGETS[actor]


def compute_risk_score(total_orders: int, total_refused: int) -> int:
    """0..100. Share of refused orders, plus a penalty for repeated refusals.

    1 refusal out of 10 -> 10 (fine). 2 refusals -> at least 50 (warning badge).
    """
    if total_orders <= 0 or total_refused <= 0:
        return 0
    ratio = round(100 * total_refused / total_orders)
    repeat_penalty = RISKY_THRESHOLD if total_refused >= 2 else 0
    return min(100, max(ratio, repeat_penalty))


def is_risky(customer: Customer) -> bool:
    return customer.risk_score >= RISKY_THRESHOLD


def transition(
    db: Session,
    order: Order,
    to_status: OrderStatus,
    actor: Actor,
    note: str | None = None,
) -> Event:
    """Validate and apply a status change, log an Event, update customer stats."""
    from_status = order.status
    if not can_transition(from_status, to_status, actor):
        log.warning(
            "order: invalid transition",
            extra={
                "order_id": order.id,
                "from": from_status.value,
                "to": to_status.value,
                "actor": actor.value,
            },
        )
        raise InvalidTransition(from_status, to_status, actor)

    order.status = to_status
    event = Event(order=order, from_status=from_status, to_status=to_status, actor=actor, note=note)
    db.add(event)

    if to_status is S.REFUSED:
        customer = order.customer
        customer.total_refused += 1
        customer.risk_score = compute_risk_score(customer.total_orders, customer.total_refused)

    db.flush()
    log.info(
        "order: status changed",
        extra={
            "order_id": order.id,
            "from": from_status.value,
            "to": to_status.value,
            "actor": actor.value,
        },
    )
    return event


def record_new_order(db: Session, order: Order) -> Event:
    """Log the creation event (from_status=None -> NEW) and bump customer.total_orders."""
    customer = order.customer
    customer.total_orders += 1
    customer.risk_score = compute_risk_score(customer.total_orders, customer.total_refused)
    event = Event(order=order, from_status=None, to_status=S.NEW, actor=Actor.BOT)
    db.add(event)
    db.flush()
    return event
