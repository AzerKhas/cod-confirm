"""Conversation logic for incoming customer messages (completed in Phase 4)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import MessageLog, Shop


def handle_incoming(db: Session, shop: Shop, msg: MessageLog) -> None:
    """Phase 3: messages are only stored. Phase 4 adds intents, replies and opt-out."""
    return None
