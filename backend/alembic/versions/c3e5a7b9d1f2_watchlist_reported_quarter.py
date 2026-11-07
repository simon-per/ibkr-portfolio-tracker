"""watchlist_reported_quarter

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
Create Date: 2026-11-07 21:00:00.000000

The watchlist re-reads its growth columns the run after a company reports, not up to a week
later: each run stores the newest quarter `.info` reports (`mostRecentQuarter`), and a later
one fetches the statements and estimates that run (`watchlist_service.new_quarter`). NULL on
arrival: the first run after the deploy records the quarter and fetches nothing extra.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3e5a7b9d1f2"
down_revision: Union[str, None] = "b2d4f6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "watchlist_items", sa.Column("reported_quarter", sa.Date(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("watchlist_items") as batch:
        batch.drop_column("reported_quarter")
