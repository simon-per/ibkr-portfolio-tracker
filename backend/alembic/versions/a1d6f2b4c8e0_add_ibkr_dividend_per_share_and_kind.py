"""add_ibkr_dividend_per_share_and_kind

Revision ID: a1d6f2b4c8e0
Revises: z9c5e1a3b7d4
Create Date: 2026-10-04 10:00:00.000000

IBKR's own per-share rate and dividend kind on each `ibkr` dividend row, parsed from
the cash line's description, so the forecast sizes from what IBKR paid rather than
from Yahoo's figure. Additive and nullable: existing rows fill in as the next
statements re-deliver them, and a row that never does keeps working from its amounts.
See the model comment.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1d6f2b4c8e0'
down_revision: Union[str, None] = 'z9c5e1a3b7d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column('dividend_payments',
                  sa.Column('per_share_native', sa.Numeric(18, 6), nullable=True))
    op.add_column('dividend_payments',
                  sa.Column('dividend_kind', sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('dividend_payments') as batch:
        batch.drop_column('dividend_kind')
        batch.drop_column('per_share_native')
