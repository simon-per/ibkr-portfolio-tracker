"""add_security_fundamentals_check_stamps

Revision ID: c9f6e3a1b5d8
Revises: b8e5d2f0a4c7
Create Date: 2026-10-18 12:00:00.000000

Bound the fundamentals pass's re-asks (GitHub issue #66): when Yahoo last answered a
security's `.info` with nothing usable, or failed with something not a rate limit, and how
many times running — the price loop's pair (`a2e8c4f6b0d3`), read through the same rule.
NULL on arrival: no security starts in a backoff, so the first pass after the deploy asks
exactly what every pass asked before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f6e3a1b5d8'
down_revision: Union[str, None] = 'b8e5d2f0a4c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMNs: SQLite does them in place, with no table rebuild.
    op.add_column('securities', sa.Column('fundamentals_checked_at', sa.DateTime(), nullable=True))
    op.add_column('securities', sa.Column('fundamentals_miss_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('securities') as batch:
        batch.drop_column('fundamentals_miss_count')
        batch.drop_column('fundamentals_checked_at')
