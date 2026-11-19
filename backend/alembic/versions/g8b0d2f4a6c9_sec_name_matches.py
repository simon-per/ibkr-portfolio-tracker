"""sec_name_matches and the annual report's period on sec_tickers

Revision ID: g8b0d2f4a6c9
Revises: f7a9c1e3b5d8
Create Date: 2026-11-19 21:00:00.000000

A 20-F filer found by its name (docs/company-financials.md, *Scope*): `sec_name_matches` keeps
the SEC filer whose name agrees with a company the baskets name under a ticker the SEC does not
list (Novartis's NOVN), and `sec_tickers` the period the filer's newest annual report ends and
when that was asked, so a filer that stopped filing (ABB, Infineon) is not taken.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "g8b0d2f4a6c9"
down_revision: Union[str, None] = "f7a9c1e3b5d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sec_tickers", sa.Column("annual_period_end", sa.Date(), nullable=True))
    op.add_column("sec_tickers", sa.Column("form_checked_at", sa.DateTime(), nullable=True))
    op.create_table(
        "sec_name_matches",
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=True),
        sa.Column("ticker", sa.String(length=20), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("isin"),
    )


def downgrade() -> None:
    op.drop_table("sec_name_matches")
    with op.batch_alter_table("sec_tickers") as batch:
        batch.drop_column("form_checked_at")
        batch.drop_column("annual_period_end")
