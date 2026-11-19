"""constituent_prices

Revision ID: f7a9c1e3b5d8
Revises: e6a8c0d2f4b7
Create Date: 2026-11-19 15:00:00.000000

Each day's basket price of another regulator's company (`constituent_prices`), so a Korean or
European company's multiples have a price without a Yahoo request. docs/company-financials.md,
*Prices without Yahoo*. Empty on arrival; the next basket refresh fills it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f7a9c1e3b5d8"
down_revision: Union[str, None] = "e6a8c0d2f4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "constituent_prices",
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fund_isin", sa.String(length=12), nullable=False),
        sa.Column("units_held", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("isin", "date"),
    )


def downgrade() -> None:
    op.drop_table("constituent_prices")
