"""Add PEARSONVUE and TRAINING_PROVIDER to the sourcetype enum

Revision ID: l5m6n7o8p9q1
Revises: k2l3m4n5o6p7
Create Date: 2026-08-14

Changes
-------
The ``sourcetype`` enum was originally created with only the seven
generic source types. The running models also define `PEARSONVUE` and
`TRAINING_PROVIDER` (voucherbot/models/source.py), so this migration
adds them to the PostgreSQL enum so those sources can be persisted by a
migration-led (production) database exactly as the models define them.

``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is used so the migration is
idempotent against development databases where ``create_all`` already
created the enum with the full value set.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "l5m6n7o8p9q1"
down_revision: Union[str, Sequence[str], None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_VALUES = ("PEARSONVUE", "TRAINING_PROVIDER")


def upgrade() -> None:
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE sourcetype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Enum values are additive and forward-compatible. DROP VALUE is only
    # supported on PostgreSQL 16+ and fails once any row uses the value, so
    # a safe downgrade is intentionally a no-op here.
    pass
