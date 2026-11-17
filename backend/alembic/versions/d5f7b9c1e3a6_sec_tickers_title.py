"""sec_tickers_title

Revision ID: d5f7b9c1e3a6
Revises: c4e6a8b0d2f4
Create Date: 2026-11-17 12:00:00.000000

The SEC's name for a ticker and the form its filer's annual report is on
(docs/company-financials.md, *Scope*): a look-through company without a US ISIN is found by
the baskets' ticker only when the SEC's name agrees and the filer reports on 10-K. NULL on
arrival; the next pass asks the ticker list once for a ticker it needs a name for. Nothing is
requested here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d5f7b9c1e3a6"
down_revision: Union[str, None] = "c4e6a8b0d2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sec_tickers", sa.Column("title", sa.String(length=200), nullable=True))
    op.add_column(
        "sec_tickers", sa.Column("annual_form", sa.String(length=10), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("sec_tickers") as batch:
        batch.drop_column("annual_form")
        batch.drop_column("title")
