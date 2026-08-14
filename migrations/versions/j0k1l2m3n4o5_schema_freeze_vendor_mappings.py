"""Schema freeze: ensure vendor_mappings table exists

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-07-23

Frozen baseline for production. Captures the ``vendor_mappings`` table so
that ``alembic upgrade head`` fully reproduces the schema from a fresh
database without relying on ORM create_all.

NOTE: this migration is a released revision and must NOT be deleted. Existing
databases are stamped at this revision; removing the file makes ``alembic
upgrade head`` fail with "No such revision". Its upgrade is a no-op on fresh
databases (``h4d5e6f7a8b9`` already creates the table) and safely skips when
the table exists.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "j0k1l2m3n4o5"
down_revision: Union[str, Sequence[str], None] = "i9j0k1l2m3n4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = inspect(bind).get_table_names()
    if "vendor_mappings" in existing:
        return

    op.create_table(
        "vendor_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url_pattern", sa.String(), nullable=True),
        sa.Column("source_name_pattern", sa.String(), nullable=True),
        sa.Column("vendor", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vendor_mappings_source_name_pattern",
        "vendor_mappings",
        ["source_name_pattern"],
        unique=True,
    )
    op.create_index(
        "ix_vendor_mappings_url_pattern",
        "vendor_mappings",
        ["url_pattern"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_vendor_mappings_url_pattern", table_name="vendor_mappings")
    op.drop_index(
        "ix_vendor_mappings_source_name_pattern", table_name="vendor_mappings"
    )
    op.drop_table("vendor_mappings")
