"""sec_companies_filings_listed_at

Revision ID: f1b3d5e7a9c2
Revises: e7a9c1d3f5b8
Create Date: 2026-11-16 12:00:00.000000

When the EDGAR pass last asked a company's filing list (`submissions`) for a filing
companyfacts skipped (docs/company-financials.md, *When companyfacts lags the filing list*;
STATUS 7a): AEP's and EXC's Q2 2026 10-Qs were read by hand on 2026-11-11, and NU's FY2025
20-F never. NULL until the first such ask. Nothing is requested here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f1b3d5e7a9c2"
down_revision: Union[str, None] = "e7a9c1d3f5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sec_companies", sa.Column("filings_listed_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("sec_companies") as batch:
        batch.drop_column("filings_listed_at")
