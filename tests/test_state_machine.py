from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.enums import Actor, OrderStatus
from app.services.orders import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    can_transition,
    compute_risk_score,
    is_risky,
    transition,
)
from tests.factories import make_order, make_shop

S = OrderStatus

# Every valid edge of the graph (from, to).
VALID_EDGES = [(src, dst) for src, targets in TRANSITIONS.items() for dst in targets]
INVALID_EDGES = [
    (src, dst) for src in OrderStatus for dst in OrderStatus if dst not in TRANSITIONS[src]
]


def test_every_status_has_an_entry() -> None:
    assert set(TRANSITIONS) == set(OrderStatus)


def test_terminal_states() -> None:
    assert TERMINAL == {S.DELIVERED, S.REFUSED, S.CANCELLED}


def test_main_happy_path(db_session: Session) -> None:
    order = make_order(db_session, make_shop(db_session))
    transition(db_session, order, S.AWAITING_CONFIRMATION, Actor.BOT)
    transition(db_session, order, S.CONFIRMED, Actor.CUSTOMER)
    transition(db_session, order, S.SHIPPED, Actor.ADMIN)
    transition(db_session, order, S.DELIVERED, Actor.ADMIN)

    assert order.status is S.DELIVERED
    path = [(e.from_status, e.to_status, e.actor) for e in order.events]
    assert path == [
        (None, S.NEW, Actor.BOT),
        (S.NEW, S.AWAITING_CONFIRMATION, Actor.BOT),
        (S.AWAITING_CONFIRMATION, S.CONFIRMED, Actor.CUSTOMER),
        (S.CONFIRMED, S.SHIPPED, Actor.ADMIN),
        (S.SHIPPED, S.DELIVERED, Actor.ADMIN),
    ]


def test_modify_loop(db_session: Session) -> None:
    order = make_order(db_session, make_shop(db_session), status=S.AWAITING_CONFIRMATION)
    transition(db_session, order, S.MODIFY_REQUESTED, Actor.CUSTOMER)
    transition(db_session, order, S.AWAITING_CONFIRMATION, Actor.ADMIN, note="address fixed")
    transition(db_session, order, S.CONFIRMED, Actor.CUSTOMER)
    assert order.status is S.CONFIRMED
    assert order.events[-2].note == "address fixed"


@pytest.mark.parametrize(("src", "dst"), VALID_EDGES)
def test_admin_can_do_every_valid_edge(db_session: Session, src: S, dst: S) -> None:
    order = make_order(db_session, make_shop(db_session), status=src)
    event = transition(db_session, order, dst, Actor.ADMIN)
    assert order.status is dst
    assert (event.from_status, event.to_status) == (src, dst)


@pytest.mark.parametrize(("src", "dst"), INVALID_EDGES)
def test_invalid_edges_are_rejected(db_session: Session, src: S, dst: S) -> None:
    order = make_order(db_session, make_shop(db_session), status=src)
    events_before = len(order.events)
    with pytest.raises(InvalidTransition):
        transition(db_session, order, dst, Actor.ADMIN)
    assert order.status is src  # unchanged
    assert len(order.events) == events_before  # nothing logged


@pytest.mark.parametrize(
    ("src", "dst", "actor"),
    [
        (S.CONFIRMED, S.SHIPPED, Actor.CUSTOMER),  # customer cannot ship
        (S.SHIPPED, S.DELIVERED, Actor.BOT),  # bot cannot mark delivered (yet)
        (S.SHIPPED, S.REFUSED, Actor.CUSTOMER),
        (S.AWAITING_CONFIRMATION, S.CONFIRMED, Actor.BOT),  # bot never confirms alone
        (S.MODIFY_REQUESTED, S.AWAITING_CONFIRMATION, Actor.CUSTOMER),  # admin edits first
    ],
)
def test_actor_permissions(src: S, dst: S, actor: Actor) -> None:
    assert dst in TRANSITIONS[src]  # the edge exists...
    assert not can_transition(src, dst, actor)  # ...but not for this actor


@pytest.mark.parametrize(
    ("src", "dst", "actor"),
    [
        (S.NEW, S.AWAITING_CONFIRMATION, Actor.BOT),
        (S.AWAITING_CONFIRMATION, S.NO_ANSWER, Actor.BOT),
        (S.AWAITING_CONFIRMATION, S.CANCELLED, Actor.CUSTOMER),
        (S.NO_ANSWER, S.CONFIRMED, Actor.CUSTOMER),  # late reply
    ],
)
def test_allowed_non_admin_moves(src: S, dst: S, actor: Actor) -> None:
    assert can_transition(src, dst, actor)


def test_refused_updates_risk(db_session: Session) -> None:
    shop = make_shop(db_session)
    first = make_order(db_session, shop, status=S.SHIPPED)
    customer = first.customer
    transition(db_session, first, S.REFUSED, Actor.ADMIN)
    assert customer.total_refused == 1
    assert customer.risk_score == 100  # 1 order, 1 refusal
    assert is_risky(customer)


@pytest.mark.parametrize(
    ("orders", "refused", "score"),
    [(0, 0, 0), (5, 0, 0), (10, 1, 10), (10, 2, 50), (4, 3, 75), (3, 5, 100)],
)
def test_compute_risk_score(orders: int, refused: int, score: int) -> None:
    assert compute_risk_score(orders, refused) == score


def test_total_orders_counted(db_session: Session) -> None:
    shop = make_shop(db_session)
    o1 = make_order(db_session, shop)
    make_order(db_session, shop, customer=o1.customer)
    assert o1.customer.total_orders == 2
