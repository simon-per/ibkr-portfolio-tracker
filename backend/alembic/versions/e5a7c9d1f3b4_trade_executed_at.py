"""trade_executed_at

Revision ID: e5a7c9d1f3b4
Revises: d4f6a8c0e2b3
Create Date: 2026-11-08 12:00:00.000000

When IBKR executed a trade (its Flex ``dateTime``), as naive UTC. A lot opened by a BUY
carries the same instant as its ``openDateTime``, which is how the lot is given the
trade's own ``tradeDate``: an Asian-session buy executes on the US Eastern evening
before its exchange-local trade date, and the lot entered the holdings a day before its
cash left. NULL on arrival, and no request: every Flex sync re-delivers the trades of
its window, so the instants fill from the next one on; a lot whose trade is older keeps
the date it has.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5a7c9d1f3b4"
down_revision: Union[str, None] = "d4f6a8c0e2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column("trades", sa.Column("executed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trades") as batch:
        batch.drop_column("executed_at")
