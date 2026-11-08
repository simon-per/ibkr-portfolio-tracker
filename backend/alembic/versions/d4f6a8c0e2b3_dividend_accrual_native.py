"""dividend_accrual_native

Revision ID: d4f6a8c0e2b3
Revises: c3e5a7b9d1f2
Create Date: 2026-11-08 10:00:00.000000

An open accrual's amounts in the payment's own currency, as IBKR states them: the gross
rate per share (``grossRate``), the gross, the tax and the net. The EUR columns froze the
rate of the day the row was stored, so every later reading of an unchanged dividend moved
with the exchange rate; these are converted at read time with the rate the rest of the
forecast is sized at. NULL on arrival, and no request of any kind: the table is replaced
wholesale on every Flex sync, so the first one after the deploy fills them, and until then
the readers fall back to the EUR columns as before.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4f6a8c0e2b3"
down_revision: Union[str, None] = "c3e5a7b9d1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ("gross_rate", "gross_amount", "withholding_tax", "net_amount")


def upgrade() -> None:
    # Plain ADD COLUMNs: SQLite does them in place, with no table rebuild.
    for name in COLUMNS:
        op.add_column(
            "dividend_accruals", sa.Column(name, sa.Numeric(18, 6), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("dividend_accruals") as batch:
        for name in reversed(COLUMNS):
            batch.drop_column(name)
