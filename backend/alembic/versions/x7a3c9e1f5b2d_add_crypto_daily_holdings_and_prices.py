"""add_crypto_daily_holdings_and_prices

Revision ID: x7a3c9e1f5b2d
Revises: w6f3b0c7d1e2
Create Date: 2026-10-02 12:00:00.000000

The crypto book computes its own history (docs/crypto.md): a per-coin quantity per UTC day
(`crypto_daily_holdings`), CoinGecko's USD price per coin per day (`crypto_coin_prices`) and
which CoinGecko coin each CoinStats identifier is (`crypto_coin_ids`).

Additive and empty on arrival. The first crypto sync after the deploy writes today's
holdings (and, with `COINGECKO_API_KEY` set, the prices); the rebuild CLI
`app.cli.crypto_rebuild_holdings` fills the days before it. `crypto_daily` stays, no longer
written, for one release of comparison. `Float` for the reason `w6f3b0c7d1e2` gives.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'x7a3c9e1f5b2d'
down_revision: Union[str, None] = 'w6f3b0c7d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'crypto_daily_holdings',
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('coin_id', sa.String(length=128), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=True),
        sa.Column('count', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint('date', 'coin_id'),
    )
    op.create_table(
        'crypto_coin_prices',
        sa.Column('coingecko_id', sa.String(length=128), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('price_usd', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('coingecko_id', 'date'),
    )
    op.create_table(
        'crypto_coin_ids',
        sa.Column('coinstats_id', sa.String(length=128), nullable=False),
        sa.Column('coingecko_id', sa.String(length=128), nullable=True),
        sa.Column('symbol', sa.String(length=32), nullable=True),
        sa.Column('method', sa.String(length=16), nullable=True),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.Column('closes_checked_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('coinstats_id'),
    )


def downgrade() -> None:
    op.drop_table('crypto_coin_ids')
    op.drop_table('crypto_coin_prices')
    op.drop_table('crypto_daily_holdings')
