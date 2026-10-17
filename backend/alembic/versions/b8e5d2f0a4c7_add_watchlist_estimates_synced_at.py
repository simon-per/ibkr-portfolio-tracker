"""add_watchlist_estimates_synced_at

Revision ID: b8e5d2f0a4c7
Revises: a7d4c1e9f3b8
Create Date: 2026-10-17 21:00:00.000000

When a watchlist item's slow Yahoo fields — `quarterly_financials` and the analyst
estimates — were last fetched, so the nightly refresh asks for them once a week and reads
`.info` alone on the other nights (GitHub issue #11, item d). NULL on arrival: the first
refresh after the deploy fetches every item in full, which is what every refresh did before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e5d2f0a4c7'
down_revision: Union[str, None] = 'a7d4c1e9f3b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column('watchlist_items', sa.Column('estimates_synced_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('watchlist_items') as batch:
        batch.drop_column('estimates_synced_at')
