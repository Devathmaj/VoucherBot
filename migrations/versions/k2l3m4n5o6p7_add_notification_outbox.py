"""Add notification_outbox table

Revision ID: k2l3m4n5o6p7
Revises: i9j0k1l2m3n4
Create Date: 2026-08-13

Changes
-------
1. Create ``notification_outbox`` — transactional outbox for voucher alert
   emails so delivery state survives commit failures and is reliably retried.
2. ``idempotency_key`` is unique: it is passed to Resend as the
   ``Idempotency-Key`` header, so replaying a pending row cannot duplicate
   an email.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k2l3m4n5o6p7"
down_revision: Union[str, Sequence[str], None] = "i9j0k1l2m3n4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "post_id",
            sa.Integer(),
            sa.ForeignKey("posts.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "SENT", "FAILED", name="notificationstatus"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_notification_outbox_idempotency_key"
        ),
    )
    op.create_index(
        "ix_notification_outbox_post_id",
        "notification_outbox",
        ["post_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_status",
        "notification_outbox",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_status", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_post_id", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    sa.Enum(name="notificationstatus").drop(op.get_bind(), checkfirst=True)
