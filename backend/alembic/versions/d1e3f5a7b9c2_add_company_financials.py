"""add_company_financials

Revision ID: d1e3f5a7b9c2
Revises: c3f9a7d1e5b2
Create Date: 2026-10-25 12:00:00.000000

The Company tab's fundamentals history from SEC EDGAR (docs/company-financials.md): the
normalised figures, and each security's CIK with the EDGAR pass's stamps. Empty on
arrival: `app.cli.fetch_company_financials` or the weekly pass fills them.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d1e3f5a7b9c2"
down_revision: Union[str, None] = "c3f9a7d1e5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "security_ciks",
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=True),
        sa.Column("sec_ticker", sa.String(length=20), nullable=True),
        sa.Column("cik_checked_at", sa.DateTime(), nullable=False),
        sa.Column("facts_checked_at", sa.DateTime(), nullable=True),
        sa.Column("facts_miss_count", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("security_id"),
    )
    op.create_index("ix_security_ciks_cik", "security_ciks", ["cik"], unique=False)
    op.create_table(
        "company_financials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_period", sa.String(length=2), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("source_concept", sa.String(length=128), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "security_id",
            "period_end",
            "fiscal_period",
            "metric",
            name="uq_company_financials_period_metric",
        ),
    )
    op.create_index(
        "ix_company_financials_security_id",
        "company_financials",
        ["security_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_company_financials_security_id", table_name="company_financials")
    op.drop_table("company_financials")
    op.drop_index("ix_security_ciks_cik", table_name="security_ciks")
    op.drop_table("security_ciks")
