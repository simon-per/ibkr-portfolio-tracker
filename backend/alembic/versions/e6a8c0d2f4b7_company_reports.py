"""company_reports

Revision ID: e6a8c0d2f4b7
Revises: d5f7b9c1e3a6
Create Date: 2026-11-17 14:00:00.000000

The other regulators' reports, each read once ever (`company_reports`: an OpenDART periodic
report or an ESEF filing, what it held), and OpenDART's company for each Korean stock code
asked about (`dart_corp_codes`). docs/company-financials.md, *South Korea* and *Europe*.
Empty on arrival; nothing is requested here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e6a8c0d2f4b7"
down_revision: Union[str, None] = "d5f7b9c1e3a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_reports",
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.String(length=80), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("accn", sa.String(length=80), nullable=True),
        sa.Column("filed", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("tagged", sa.JSON(), nullable=False),
        sa.Column("non_standard", sa.Integer(), server_default="0", nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("company_id", "report_id"),
    )
    op.create_table(
        "dart_corp_codes",
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("stock_code", sa.String(length=6), nullable=False),
        sa.Column("corp_code", sa.String(length=8), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("isin"),
    )
    op.create_index(
        "ix_dart_corp_codes_corp_code", "dart_corp_codes", ["corp_code"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_dart_corp_codes_corp_code", table_name="dart_corp_codes")
    op.drop_table("dart_corp_codes")
    op.drop_table("company_reports")
