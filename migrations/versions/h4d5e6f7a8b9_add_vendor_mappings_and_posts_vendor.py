"""Add vendor_mappings table and posts.vendor column

Revision ID: h4d5e6f7a8b9
Revises: g3b9c0d1e2f3
Create Date: 2026-07-23

Changes
-------
1. Create ``vendor_mappings`` table with ``url_pattern`` and
   ``source_name_pattern`` (both nullable, at least one should be set).
2. Add ``vendor`` column on ``posts`` (nullable, indexed).
3. Update ``voucher_posts`` view to prefer ``posts.vendor`` over
   ``ai_result->>'vendor'``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "g3b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. vendor_mappings table ──────────────────────────────────────────────
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

    # ── 2. posts.vendor column ────────────────────────────────────────────────
    op.add_column(
        "posts",
        sa.Column("vendor", sa.String(), nullable=True),
    )
    op.create_index("ix_posts_vendor", "posts", ["vendor"], unique=False)

    # ── 3. Update voucher_posts view ──────────────────────────────────────────
    # CREATE OR REPLACE VIEW cannot reorder view columns, so drop first.
    op.execute(sa.text("DROP VIEW IF EXISTS voucher_posts"))
    op.execute(
        sa.text(
            """
            CREATE VIEW voucher_posts AS
            SELECT
                p.id,
                p.source_id,
                p.external_id,
                p.url,
                p.title,
                p.content,
                p.summary,
                p.author,
                p.published_at,
                p.status,
                p.score,
                p.raw_data,
                p.ai_result,
                p.content_hash,
                p.event_id,
                p.is_notified,
                p.vendor,
                p.created_at,
                p.updated_at,
                p.ai_result->>'promotion_name' AS promotion_name,
                p.ai_result->>'promotion_type' AS promotion_type,
                p.ai_result->>'voucher_code' AS voucher_code,
                p.ai_result->>'discount' AS discount,
                p.ai_result->>'registration_url' AS registration_url,
                p.ai_result->>'reason' AS reason,
                CASE
                    WHEN p.ai_result ? 'confidence'
                         AND (p.ai_result->>'confidence') ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (p.ai_result->>'confidence')::double precision
                    ELSE NULL
                END AS confidence
            FROM posts p
            WHERE (p.ai_result->>'is_voucher') = 'true'
              AND p.status = 'PROCESSED'
            """
        )
    )


def downgrade() -> None:
    # CREATE OR REPLACE VIEW cannot reorder view columns, so drop first.
    op.execute(sa.text("DROP VIEW IF EXISTS voucher_posts"))
    op.execute(
        sa.text(
            """
            CREATE VIEW voucher_posts AS
            SELECT
                p.id,
                p.source_id,
                p.external_id,
                p.url,
                p.title,
                p.content,
                p.summary,
                p.author,
                p.published_at,
                p.status,
                p.score,
                p.raw_data,
                p.ai_result,
                p.content_hash,
                p.event_id,
                p.is_notified,
                p.created_at,
                p.updated_at,
                p.ai_result->>'vendor' AS vendor,
                p.ai_result->>'promotion_name' AS promotion_name,
                p.ai_result->>'promotion_type' AS promotion_type,
                p.ai_result->>'voucher_code' AS voucher_code,
                p.ai_result->>'discount' AS discount,
                p.ai_result->>'registration_url' AS registration_url,
                p.ai_result->>'reason' AS reason,
                CASE
                    WHEN p.ai_result ? 'confidence'
                         AND (p.ai_result->>'confidence') ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN (p.ai_result->>'confidence')::double precision
                    ELSE NULL
                END AS confidence
            FROM posts p
            WHERE (p.ai_result->>'is_voucher') = 'true'
              AND p.status = 'PROCESSED'
            """
        )
    )

    op.drop_index("ix_posts_vendor", table_name="posts")
    op.drop_column("posts", "vendor")
    op.drop_index(
        "ix_vendor_mappings_source_name_pattern", table_name="vendor_mappings"
    )
    op.drop_index("ix_vendor_mappings_url_pattern", table_name="vendor_mappings")
    op.drop_table("vendor_mappings")
