"""meta pixel and conversions api

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.add_column(sa.Column("meta_pixel_id", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("meta_capi_token", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("meta_test_event_code", sa.String(length=40), nullable=True))
        batch_op.add_column(
            sa.Column(
                "meta_event_on", sa.String(length=12), nullable=False, server_default="confirmed"
            )
        )
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("ad_consent", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("ad_meta", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("capi_state", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("capi_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("capi_sent_at")
        batch_op.drop_column("capi_state")
        batch_op.drop_column("ad_meta")
        batch_op.drop_column("ad_consent")
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.drop_column("meta_event_on")
        batch_op.drop_column("meta_test_event_code")
        batch_op.drop_column("meta_capi_token")
        batch_op.drop_column("meta_pixel_id")
