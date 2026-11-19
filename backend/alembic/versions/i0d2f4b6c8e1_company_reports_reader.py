"""company_reports.reader

Revision ID: i0d2f4b6c8e1
Revises: h9c1e3a5b7d0
Create Date: 2026-11-19 23:00:00.000000

Which IFRS concept set an ESEF report's read kept (docs/company-financials.md, *Europe*): the
reports read before 2026-11-19's twins (continuing EPS, net interest income) are read again.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "i0d2f4b6c8e1"
down_revision: Union[str, None] = "h9c1e3a5b7d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("company_reports", sa.Column("reader", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("company_reports") as batch:
        batch.drop_column("reader")
