"""company_classification

Revision ID: b3d5f7a9c1e2
Revises: a2c4e6f8b0d1
Create Date: 2026-11-16 21:00:00.000000

The classification's two SEC inputs beside the SIC code (docs/company-financials.md,
*Classification*): `owner_org`, the office reviewing the company's filings, kept from the
submissions document the pass already reads monthly; `statement_model`, the business model the
newest annual report's concepts show, written by each companyfacts fetch. Every stored profile
is marked unread (`submissions_checked_at` NULL), so the next weekly pass reads each company's
submissions document once — about 130 small requests — instead of waiting up to a month for
`owner_org`. Nothing is requested here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3d5f7a9c1e2"
down_revision: Union[str, None] = "a2c4e6f8b0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sec_companies", sa.Column("owner_org", sa.String(length=100), nullable=True))
    op.add_column(
        "sec_companies", sa.Column("statement_model", sa.String(length=16), nullable=True)
    )
    op.execute("UPDATE sec_companies SET submissions_checked_at = NULL")


def downgrade() -> None:
    with op.batch_alter_table("sec_companies") as batch:
        batch.drop_column("statement_model")
        batch.drop_column("owner_org")
