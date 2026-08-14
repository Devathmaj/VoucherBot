"""Reconcile databases built by create_all to the migration-defined schema

Revision ID: m6n7o8p9q0r1
Revises: l5m6n7o8p9q1
Create Date: 2026-08-14

Databases that were previously created via ``init_db()``/``create_all`` were
stamped at head without ever running the migration chain, so they are missing
two things the migrations (and the running models) define:

1. The ``voucher_posts`` view (views are never created by ``create_all``).
2. Timezone-aware ``vendor_mappings.created_at`` / ``updated_at`` columns
   (the models declare ``DateTime(timezone=True)``).

This migration reconciles those databases in place so ``alembic check`` is
clean. On a fresh database it is a no-op: the view already exists and the
columns are already timezone-aware.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "m6n7o8p9q0r1"
down_revision: Union[str, Sequence[str], None] = "l5m6n7o8p9q1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. voucher_posts view — CREATE OR REPLACE is idempotent.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE VIEW voucher_posts AS
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

    # 2. Timezone-aware timestamps on vendor_mappings (no-op when already tz).
    op.alter_column(
        "vendor_mappings",
        "created_at",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
    op.alter_column(
        "vendor_mappings",
        "updated_at",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "vendor_mappings",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        existing_nullable=False,
    )
    op.alter_column(
        "vendor_mappings",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        existing_nullable=False,
    )
