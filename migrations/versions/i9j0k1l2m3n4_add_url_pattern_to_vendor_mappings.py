"""Add url_pattern column to vendor_mappings

Revision ID: i9j0k1l2m3n4
Revises: h4d5e6f7a8b9
Create Date: 2026-07-23

Changes
-------
1. Add ``url_pattern`` column to ``vendor_mappings`` (nullable).
2. Make ``source_name_pattern`` nullable.
3. Add unique index on ``url_pattern``.

NOTE: the parent migration ``h4d5e6f7a8b9`` already creates ``vendor_mappings``
with ``url_pattern`` and its unique index, so the steps here are applied
defensively (only when missing) to stay correct on a fresh ``alembic upgrade``
while still repairing legacy databases that predate the column.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "i9j0k1l2m3n4"
down_revision: Union[str, Sequence[str], None] = "h4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("vendor_mappings")}
    indexes = {ix["name"] for ix in inspector.get_indexes("vendor_mappings")}

    if "url_pattern" not in columns:
        op.add_column(
            "vendor_mappings",
            sa.Column("url_pattern", sa.String(), nullable=True),
        )
    op.alter_column(
        "vendor_mappings",
        "source_name_pattern",
        existing_type=sa.String(),
        nullable=True,
    )
    if "ix_vendor_mappings_url_pattern" not in indexes:
        op.create_index(
            "ix_vendor_mappings_url_pattern",
            "vendor_mappings",
            ["url_pattern"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("vendor_mappings")}
    indexes = {ix["name"] for ix in inspector.get_indexes("vendor_mappings")}

    if "ix_vendor_mappings_url_pattern" in indexes:
        op.drop_index("ix_vendor_mappings_url_pattern", table_name="vendor_mappings")
    op.alter_column(
        "vendor_mappings",
        "source_name_pattern",
        existing_type=sa.String(),
        nullable=False,
    )
    if "url_pattern" in columns:
        op.drop_column("vendor_mappings", "url_pattern")
