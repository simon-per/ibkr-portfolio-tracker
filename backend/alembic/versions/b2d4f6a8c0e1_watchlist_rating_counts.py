"""watchlist_rating_counts

Revision ID: b2d4f6a8c0e1
Revises: a7c9e1f3b5d8
Create Date: 2026-11-07 20:00:00.000000

The watchlist shows each name's analyst recommendation counts, labelled by
`analyst_rating.consensus_of` like the positions table, instead of Yahoo's own
`recommendationKey`. A held listing's and a Company-tab company's counts are read from
the rows their passes keep; these columns hold the counts the watchlist asks Yahoo for
itself, for any other ticker, and when it last asked. NULL on arrival: the first run after
the deploy asks each such ticker once.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2d4f6a8c0e1"
down_revision: Union[str, None] = "a7c9e1f3b5d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COUNTS = ("strong_buy", "buy", "hold", "sell", "strong_sell")


def upgrade() -> None:
    # Plain ADD COLUMNs: SQLite does them in place, with no table rebuild.
    for name in COUNTS:
        op.add_column("watchlist_items", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column(
        "watchlist_items", sa.Column("ratings_synced_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("watchlist_items") as batch:
        batch.drop_column("ratings_synced_at")
        for name in reversed(COUNTS):
            batch.drop_column(name)
