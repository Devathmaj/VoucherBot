"""Add missing created_at/updated_at to keywords and pipeline_lock

Revision ID: o9p8q7r6s5t4
Revises: m6n7o8p9q0r1
Create Date: 2026-08-15

The ``Base`` model declares ``created_at``/``updated_at`` on every mapped
table, but the ``keywords`` (g3b9c0d1e2f3) and ``pipeline_lock``
(e1c2d3a4b5f6) creation migrations omitted them.  The ORM therefore emits
SELECTs referencing ``keywords.created_at`` / ``pipeline_lock.created_at``
which never existed, failing the scheduler with ``UndefinedColumnError``.
This migration backfills the missing columns to match the models.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "o9p8q7r6s5t4"
down_revision: Union[str, Sequence[str], None] = "m6n7o8p9q0r1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("keywords", "pipeline_lock"):
        columns = {col["name"] for col in inspect(bind).get_columns(table)}
        if "created_at" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "created_at",
                    sa.DateTime(),
                    nullable=False,
                    server_default=sa.func.now(),
                ),
            )
        if "updated_at" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "updated_at",
                    sa.DateTime(),
                    nullable=False,
                    server_default=sa.func.now(),
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("pipeline_lock", "keywords"):
        columns = {col["name"] for col in inspect(bind).get_columns(table)}
        if "updated_at" in columns:
            op.drop_column(table, "updated_at")
        if "created_at" in columns:
            op.drop_column(table, "created_at")
