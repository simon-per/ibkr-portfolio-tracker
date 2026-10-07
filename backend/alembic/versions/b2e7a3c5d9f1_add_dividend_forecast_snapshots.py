"""add_dividend_forecast_snapshots

Revision ID: b2e7a3c5d9f1
Revises: a1d6f2b4c8e0
Create Date: 2026-10-07 05:00:00.000000

One row per day of what the dividend forecast said (next twelve months, trailing
twelve months), so the forward figure has a history. Additive and empty on arrival;
the scheduled jobs fill it. See the model docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2e7a3c5d9f1'
down_revision: Union[str, None] = 'a1d6f2b4c8e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dividend_forecast_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_date', sa.Date(), nullable=False),
        sa.Column('next_12m_eur', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('ttm_net_eur', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('snapshot_date'),
    )
    op.create_index(
        op.f('ix_dividend_forecast_snapshots_id'), 'dividend_forecast_snapshots', ['id']
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_dividend_forecast_snapshots_id'), table_name='dividend_forecast_snapshots'
    )
    op.drop_table('dividend_forecast_snapshots')
