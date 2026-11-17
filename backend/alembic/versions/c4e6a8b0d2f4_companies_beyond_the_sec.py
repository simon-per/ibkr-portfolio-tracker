"""companies_beyond_the_sec

Revision ID: c4e6a8b0d2f4
Revises: b3d5f7a9c1e2
Create Date: 2026-11-17 10:00:00.000000

One key for a company whose figures come from another regulator than the SEC
(docs/company-financials.md, *Markets beyond the SEC*): `companies` (a surrogate id; the
public key `dart:<corp_code>` or `lei:<LEI>`), and `company_financials.company_id` beside the
CIK — exactly one of the two per row. The SEC rows are not touched: `cik` only becomes
nullable. Nothing is requested here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e6a8b0d2f4"
down_revision: Union[str, None] = "b3d5f7a9c1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scheme", sa.String(length=8), nullable=False),
        sa.Column("native_id", sa.String(length=40), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("industry_code", sa.String(length=10), nullable=True),
        sa.Column("industry", sa.String(length=200), nullable=True),
        sa.Column("fiscal_year_end", sa.String(length=4), nullable=True),
        sa.Column("statement_model", sa.String(length=16), nullable=True),
        sa.Column("profile_checked_at", sa.DateTime(), nullable=True),
        sa.Column("universe_seen_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("facts_checked_at", sa.DateTime(), nullable=True),
        sa.Column("facts_miss_count", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scheme", "native_id", name="uq_companies_scheme_native_id"
        ),
    )
    with op.batch_alter_table("company_financials") as batch:
        batch.alter_column("cik", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("company_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_company_financials_company_id_companies",
            "companies",
            ["company_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_company_financials_company_period_metric",
            ["company_id", "period_end", "fiscal_period", "metric"],
        )
        batch.create_check_constraint(
            "ck_company_financials_one_company",
            "(cik IS NULL) <> (company_id IS NULL)",
        )
        batch.create_index("ix_company_financials_company_id", ["company_id"])


def downgrade() -> None:
    # The other regulators' figures have nowhere to go without the table.
    op.execute("DELETE FROM company_financials WHERE company_id IS NOT NULL")
    with op.batch_alter_table("company_financials") as batch:
        batch.drop_index("ix_company_financials_company_id")
        batch.drop_constraint("ck_company_financials_one_company", type_="check")
        batch.drop_constraint(
            "uq_company_financials_company_period_metric", type_="unique"
        )
        batch.drop_constraint(
            "fk_company_financials_company_id_companies", type_="foreignkey"
        )
        batch.drop_column("company_id")
        batch.alter_column("cik", existing_type=sa.Integer(), nullable=False)
    op.drop_table("companies")
