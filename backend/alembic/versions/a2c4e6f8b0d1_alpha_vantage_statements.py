"""alpha_vantage_statements

Revision ID: a2c4e6f8b0d1
Revises: f1b3d5e7a9c2
Create Date: 2026-11-16 18:00:00.000000

The Company tab's last source (docs/company-financials.md, *Alpha Vantage, last*):
`vendor_statements` keeps what Alpha Vantage answered for a company's statements, so every
rewrite of its rows applies the proven fill again without asking; `sec_companies` gains the
ask's stamp and miss count (weekly at most, longer on the shared doubling while it fills
nothing). Nothing is requested here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2c4e6f8b0d1"
down_revision: Union[str, None] = "f1b3d5e7a9c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sec_companies", sa.Column("vendor_checked_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "sec_companies", sa.Column("vendor_miss_count", sa.Integer(), nullable=True)
    )
    op.create_table(
        "vendor_statements",
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("function", sa.String(length=32), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cik"], ["sec_companies.cik"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cik", "function"),
    )


def downgrade() -> None:
    op.drop_table("vendor_statements")
    with op.batch_alter_table("sec_companies") as batch:
        batch.drop_column("vendor_miss_count")
        batch.drop_column("vendor_checked_at")
