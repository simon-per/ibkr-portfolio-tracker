"""company_financials_by_cik

Revision ID: e5f7a9b1c3d6
Revises: d1e3f5a7b9c2
Create Date: 2026-10-25 15:00:00.000000

The Company tab's figures move from one set per security to one set per SEC filer
(docs/company-financials.md), so a Nasdaq-100 constituent the account does not hold can be
stored without a `securities` row. `sec_companies` takes the pass's stamps off
`security_ciks`, `sec_tickers` keeps the SEC's answer for each constituent's ticker; the
stored rows are kept, one copy per company (GOOG and GOOGL carried the same rows twice).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5f7a9b1c3d6"
down_revision: Union[str, None] = "d1e3f5a7b9c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FIGURE_COLUMNS = (
    "period_end, fiscal_year, fiscal_period, metric, value, unit, source_concept, "
    "filed, fetched_at"
)


def _figures_table(name: str, key: str, target: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(key, sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_period", sa.String(length=2), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("source_concept", sa.String(length=128), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint([key], [target], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            key,
            "period_end",
            "fiscal_period",
            "metric",
            name="uq_company_financials_period_metric",
        ),
    )


def upgrade() -> None:
    op.create_table(
        "sec_companies",
        sa.Column("cik", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("universe_seen_at", sa.DateTime(), nullable=True),
        sa.Column("facts_checked_at", sa.DateTime(), nullable=True),
        sa.Column("facts_miss_count", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("cik"),
    )
    op.create_table(
        "sec_tickers",
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("ticker"),
    )
    op.execute(
        "INSERT INTO sec_companies (cik, ticker, fetched_at, facts_checked_at, "
        "facts_miss_count) "
        "SELECT cik, MIN(sec_ticker), MAX(fetched_at), MAX(facts_checked_at), "
        "MAX(facts_miss_count) FROM security_ciks WHERE cik IS NOT NULL GROUP BY cik"
    )

    op.drop_index("ix_company_financials_security_id", table_name="company_financials")
    op.rename_table("company_financials", "company_financials_by_security")
    _figures_table("company_financials", "cik", "sec_companies.cik")
    op.create_index(
        "ix_company_financials_cik", "company_financials", ["cik"], unique=False
    )
    # One copy per company: the listing written first (lowest row id) of each figure.
    op.execute(
        f"INSERT INTO company_financials (cik, {_FIGURE_COLUMNS}) "
        f"SELECT c.cik, {', '.join('f.' + col.strip() for col in _FIGURE_COLUMNS.split(','))} "
        "FROM company_financials_by_security f "
        "JOIN security_ciks c ON c.security_id = f.security_id "
        "WHERE f.id IN ("
        "  SELECT MIN(f2.id) FROM company_financials_by_security f2"
        "  JOIN security_ciks c2 ON c2.security_id = f2.security_id"
        "  WHERE c2.cik IS NOT NULL"
        "  GROUP BY c2.cik, f2.period_end, f2.fiscal_period, f2.metric)"
    )
    op.drop_table("company_financials_by_security")

    with op.batch_alter_table("security_ciks") as batch:
        batch.drop_column("fetched_at")
        batch.drop_column("facts_miss_count")
        batch.drop_column("facts_checked_at")


def downgrade() -> None:
    with op.batch_alter_table("security_ciks") as batch:
        batch.add_column(sa.Column("facts_checked_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("facts_miss_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("fetched_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE security_ciks SET "
        "facts_checked_at = (SELECT s.facts_checked_at FROM sec_companies s "
        "  WHERE s.cik = security_ciks.cik), "
        "facts_miss_count = (SELECT s.facts_miss_count FROM sec_companies s "
        "  WHERE s.cik = security_ciks.cik), "
        "fetched_at = (SELECT s.fetched_at FROM sec_companies s "
        "  WHERE s.cik = security_ciks.cik)"
    )

    op.drop_index("ix_company_financials_cik", table_name="company_financials")
    op.rename_table("company_financials", "company_financials_by_cik")
    _figures_table("company_financials", "security_id", "securities.id")
    op.create_index(
        "ix_company_financials_security_id",
        "company_financials",
        ["security_id"],
        unique=False,
    )
    # Back under every held listing of the company; a constituent nobody holds is dropped.
    op.execute(
        f"INSERT INTO company_financials (security_id, {_FIGURE_COLUMNS}) "
        f"SELECT c.security_id, {', '.join('f.' + col.strip() for col in _FIGURE_COLUMNS.split(','))} "
        "FROM company_financials_by_cik f JOIN security_ciks c ON c.cik = f.cik"
    )
    op.drop_table("company_financials_by_cik")
    op.drop_table("sec_tickers")
    op.drop_table("sec_companies")
