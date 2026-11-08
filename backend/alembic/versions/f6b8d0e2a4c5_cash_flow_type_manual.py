"""cash_flow_type_manual

Revision ID: f6b8d0e2a4c5
Revises: e5a7c9d1f3b4
Create Date: 2026-11-08 13:00:00.000000

Whether a cash flow's type was set by hand (`manage_cash_flows reclassify`). The Flex
ingest re-delivers a row for as long as it sits in the statement window (about a month)
and wrote its own type over the owner's each time, so a reclassification lasted a day.
NULL on arrival: no row was reclassified in a way that survived, so none is marked, and
nothing is requested.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6b8d0e2a4c5"
down_revision: Union[str, None] = "e5a7c9d1f3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column("cash_flows", sa.Column("flow_type_manual", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cash_flows") as batch:
        batch.drop_column("flow_type_manual")
