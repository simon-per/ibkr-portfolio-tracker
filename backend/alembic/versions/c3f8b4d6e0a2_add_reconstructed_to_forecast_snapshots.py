"""add_reconstructed_to_forecast_snapshots

Revision ID: c3f8b4d6e0a2
Revises: b2e7a3c5d9f1
Create Date: 2026-10-12 18:00:00.000000

Marks a forecast snapshot that was replayed for a past day
(`app.cli.backfill_dividend_forecast`) apart from one recorded on its own day. Every
existing row was recorded, so the default is false. See the model docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f8b4d6e0a2'
down_revision: Union[str, None] = 'b2e7a3c5d9f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column(
        'dividend_forecast_snapshots',
        sa.Column('reconstructed', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    with op.batch_alter_table('dividend_forecast_snapshots') as batch:
        batch.drop_column('reconstructed')
