"""voice calls

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-22 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("voice_calls_on", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("call_state", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("call_conversation_id", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_orders_call_conversation_id", ["call_conversation_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ix_orders_call_conversation_id")
        batch_op.drop_column("call_conversation_id")
        batch_op.drop_column("call_state")
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.drop_column("voice_calls_on")
