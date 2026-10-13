"""add_dividend_forecast_snapshot_lines

Revision ID: d4a9c5e7f1b3
Revises: c3f8b4d6e0a2
Create Date: 2026-10-13 12:00:00.000000

One row per held security per forecast snapshot — share count, its part of the next
twelve months, and the rate it was sized at — so the change between two days can be
split into shares bought, payout per share and currency. Additive and empty on arrival;
`app.cli.backfill_dividend_forecast` fills the days already stored. See the model
docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a9c5e7f1b3'
down_revision: Union[str, None] = 'c3f8b4d6e0a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dividend_forecast_snapshot_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_date', sa.Date(), nullable=False),
        sa.Column('security_id', sa.Integer(), nullable=False),
        sa.Column('shares', sa.Numeric(18, 6), nullable=False),
        sa.Column('next_12m_eur', sa.Numeric(18, 6), nullable=False),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('fx_to_eur', sa.Numeric(18, 8), nullable=True),
        sa.Column('reconstructed', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.ForeignKeyConstraint(['security_id'], ['securities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'snapshot_date', 'security_id', name='uix_dividend_forecast_snapshot_line'
        ),
    )
    op.create_index(
        op.f('ix_dividend_forecast_snapshot_lines_id'),
        'dividend_forecast_snapshot_lines', ['id'],
    )
    op.create_index(
        op.f('ix_dividend_forecast_snapshot_lines_snapshot_date'),
        'dividend_forecast_snapshot_lines', ['snapshot_date'],
    )
    op.create_index(
        op.f('ix_dividend_forecast_snapshot_lines_security_id'),
        'dividend_forecast_snapshot_lines', ['security_id'],
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_dividend_forecast_snapshot_lines_security_id'),
                  table_name='dividend_forecast_snapshot_lines')
    op.drop_index(op.f('ix_dividend_forecast_snapshot_lines_snapshot_date'),
                  table_name='dividend_forecast_snapshot_lines')
    op.drop_index(op.f('ix_dividend_forecast_snapshot_lines_id'),
                  table_name='dividend_forecast_snapshot_lines')
    op.drop_table('dividend_forecast_snapshot_lines')
