"""company_prices

Revision ID: f6a8b0c2d4e7
Revises: e5f7a9b1c3d6
Create Date: 2026-10-25 19:00:00.000000

The Company tab's P/E for every company it lists (docs/company-financials.md, *Prices*):
Yahoo's weekly closes and split events per SEC filer, and the price pass's stamps on
`sec_companies`. New tables and nullable columns only; nothing existing is rewritten.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6a8b0c2d4e7"
down_revision: Union[str, None] = "e5f7a9b1c3d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_prices",
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.ForeignKeyConstraint(["cik"], ["sec_companies.cik"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cik", "date"),
    )
    op.create_table(
        "company_splits",
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["cik"], ["sec_companies.cik"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cik", "date"),
    )
    with op.batch_alter_table("sec_companies") as batch:
        batch.add_column(sa.Column("prices_fetched_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("prices_checked_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("prices_miss_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sec_companies") as batch:
        batch.drop_column("prices_miss_count")
        batch.drop_column("prices_checked_at")
        batch.drop_column("prices_fetched_at")
    op.drop_table("company_splits")
    op.drop_table("company_prices")
