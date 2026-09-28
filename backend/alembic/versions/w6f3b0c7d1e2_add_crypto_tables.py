"""add_crypto_tables

Revision ID: w6f3b0c7d1e2
Revises: v5e2a9b6c0d1
Create Date: 2026-09-28 12:00:00.000000

The crypto book from CoinStats (docs/crypto.md): one row per successful sync, that
sync's holdings, and CoinStats' daily value and P&L history.

Additive and empty on arrival. Nothing on the stock side reads these tables — that is
the separation the feature is built on (see `app/models/crypto.py`) — and they stay
empty until `COIN_STATS_API_KEY` and `COIN_STATS_SHARE_TOKEN` are set and a crypto sync
runs. `Float` rather than the stock tables' `Numeric(18, 6)`: SQLite reads a Numeric back
rounded to its scale, which would zero a sub-micro-dollar token price.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'w6f3b0c7d1e2'
down_revision: Union[str, None] = 'v5e2a9b6c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'crypto_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('taken_at', sa.DateTime(), nullable=False),
        sa.Column('total_value_usd', sa.Float(), nullable=True),
        sa.Column('defi_value_usd', sa.Float(), nullable=True),
        sa.Column('total_cost_usd', sa.Float(), nullable=True),
        sa.Column('unrealized_pl_usd', sa.Float(), nullable=True),
        sa.Column('unrealized_pl_pct', sa.Float(), nullable=True),
        sa.Column('realized_pl_usd', sa.Float(), nullable=True),
        sa.Column('realized_pl_pct', sa.Float(), nullable=True),
        sa.Column('all_time_pl_usd', sa.Float(), nullable=True),
        sa.Column('all_time_pl_pct', sa.Float(), nullable=True),
        sa.Column('pl_24h_usd', sa.Float(), nullable=True),
        sa.Column('valued_count', sa.Integer(), nullable=False),
        sa.Column('spam_count', sa.Integer(), nullable=False),
        sa.Column('unpriced_count', sa.Integer(), nullable=False),
        sa.Column('coin_pages', sa.Integer(), nullable=True),
        sa.Column('credits_remaining', sa.Integer(), nullable=True),
        sa.Column('credits_total', sa.Integer(), nullable=True),
        sa.Column('credits_plan', sa.String(length=32), nullable=True),
        sa.Column('credits_spent', sa.Integer(), nullable=True),
        sa.Column('warnings', sa.JSON(), nullable=True),
        sa.Column('history_status', sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_crypto_snapshots_id'), 'crypto_snapshots', ['id'])
    op.create_index(op.f('ix_crypto_snapshots_taken_at'), 'crypto_snapshots', ['taken_at'])

    op.create_table(
        'crypto_holdings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_id', sa.Integer(), nullable=False),
        sa.Column('coin_id', sa.String(length=128), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=True),
        sa.Column('name', sa.String(length=128), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('is_fiat', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('count', sa.Float(), nullable=False),
        sa.Column('price_usd', sa.Float(), nullable=True),
        sa.Column('value_usd', sa.Float(), nullable=True),
        sa.Column('total_cost_usd', sa.Float(), nullable=True),
        sa.Column('avg_buy_usd', sa.Float(), nullable=True),
        sa.Column('unrealized_pl_usd', sa.Float(), nullable=True),
        sa.Column('unrealized_pl_pct', sa.Float(), nullable=True),
        sa.Column('realized_pl_usd', sa.Float(), nullable=True),
        sa.Column('pl_24h_usd', sa.Float(), nullable=True),
        sa.Column('change_24h_pct', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ['snapshot_id'], ['crypto_snapshots.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('snapshot_id', 'coin_id', name='uix_crypto_holding_snapshot_coin'),
    )
    op.create_index(op.f('ix_crypto_holdings_id'), 'crypto_holdings', ['id'])
    op.create_index(op.f('ix_crypto_holdings_snapshot_id'), 'crypto_holdings', ['snapshot_id'])

    op.create_table(
        'crypto_daily',
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('value_usd', sa.Float(), nullable=True),
        sa.Column('pnl_usd', sa.Float(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('date'),
    )


def downgrade() -> None:
    op.drop_table('crypto_daily')
    op.drop_index(op.f('ix_crypto_holdings_snapshot_id'), table_name='crypto_holdings')
    op.drop_index(op.f('ix_crypto_holdings_id'), table_name='crypto_holdings')
    op.drop_table('crypto_holdings')
    op.drop_index(op.f('ix_crypto_snapshots_taken_at'), table_name='crypto_snapshots')
    op.drop_index(op.f('ix_crypto_snapshots_id'), table_name='crypto_snapshots')
    op.drop_table('crypto_snapshots')
