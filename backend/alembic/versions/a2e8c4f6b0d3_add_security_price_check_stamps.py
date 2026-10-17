"""add_security_price_check_stamps

Revision ID: a2e8c4f6b0d3
Revises: f6c2e8a4b1d9
Create Date: 2026-10-17 21:00:00.000000

Bound the price loop's re-asks (GitHub issue #10): when every provider last came back
empty for a security and how many times running, and when Alpha Vantage was last asked
about it. NULL on arrival: no security starts in a backoff, so the first pass after the
deploy asks exactly what every pass asked before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2e8c4f6b0d3'
down_revision: Union[str, None] = 'f6c2e8a4b1d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMNs: SQLite does them in place, with no table rebuild.
    op.add_column('securities', sa.Column('price_checked_at', sa.DateTime(), nullable=True))
    op.add_column('securities', sa.Column('price_miss_count', sa.Integer(), nullable=True))
    op.add_column('securities', sa.Column('alpha_vantage_asked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('securities') as batch:
        batch.drop_column('alpha_vantage_asked_at')
        batch.drop_column('price_miss_count')
        batch.drop_column('price_checked_at')
