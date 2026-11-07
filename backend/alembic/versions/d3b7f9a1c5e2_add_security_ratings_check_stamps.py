"""add_security_ratings_check_stamps

Revision ID: d3b7f9a1c5e2
Revises: f6a8b0c2d4e7
Create Date: 2026-11-07 12:00:00.000000

Bound the analyst ratings pass's re-asks: when its `recommendations` fetch last failed with
something not a rate limit, and how many times running — the fundamentals pair
(`c9f6e3a1b5d8`), read through the same rule. A failed fetch used to leave the security
unstamped, so every pass asked again for good. NULL on arrival: no security starts in a
backoff, so the first pass after the deploy asks exactly what every pass asked before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3b7f9a1c5e2'
down_revision: Union[str, None] = 'f6a8b0c2d4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMNs: SQLite does them in place, with no table rebuild.
    op.add_column('securities', sa.Column('ratings_checked_at', sa.DateTime(), nullable=True))
    op.add_column('securities', sa.Column('ratings_miss_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('securities') as batch:
        batch.drop_column('ratings_miss_count')
        batch.drop_column('ratings_checked_at')
