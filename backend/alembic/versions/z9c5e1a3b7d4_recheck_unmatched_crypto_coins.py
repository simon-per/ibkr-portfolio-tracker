"""recheck_unmatched_crypto_coins

Revision ID: z9c5e1a3b7d4
Revises: y8b4d0f2a6c3
Create Date: 2026-10-03 11:00:00.000000

Data only: forget every CoinStats coin stored as "no CoinGecko match", so the next crypto
sync asks again under the price-checked mapping (`pick_by_price`) instead of waiting out
`MAPPING_RECHECK_DAYS`. One-off and bounded: a coin still unmatched afterwards is stored
again and re-asked weekly, as before. Matched rows are untouched.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'z9c5e1a3b7d4'
down_revision: Union[str, None] = 'y8b4d0f2a6c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM crypto_coin_ids WHERE coingecko_id IS NULL")


def downgrade() -> None:
    # Nothing to restore: the rows were a cache of a failed lookup.
    pass
