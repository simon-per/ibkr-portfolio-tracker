"""add_level_to_forecast_snapshot_lines

Revision ID: e5b1d7f3a9c2
Revises: d4a9c5e7f1b3
Create Date: 2026-10-17 12:00:00.000000

The payer's level beside each forecast line (`dividend_forecast.payout_level`), so the
history can tell a raise from a payment entering or leaving the 365-day window. Nullable
and empty on arrival; `app.cli.backfill_dividend_forecast` fills the days already stored.
See the model docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b1d7f3a9c2'
down_revision: Union[str, None] = 'd4a9c5e7f1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column(
        'dividend_forecast_snapshot_lines',
        sa.Column('level_eur', sa.Numeric(18, 8), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table('dividend_forecast_snapshot_lines') as batch:
        batch.drop_column('level_eur')
