"""sec_filing_reads

Revision ID: a7c9e1f3b5d8
Revises: f1a3c5e7b9d2
Create Date: 2026-11-07 18:00:00.000000

What one filing's XBRL instance held that companyfacts leaves out — a dividend per share
tagged only on a dimension member (docs/company-financials.md, *Filing instances*) — one
row per filing read, so each is read once ever. A new table; nothing existing is rewritten.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c9e1f3b5d8"
down_revision: Union[str, None] = "f1a3c5e7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sec_filing_reads",
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("accn", sa.String(length=25), nullable=False),
        sa.Column("form", sa.String(length=10), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cik"], ["sec_companies.cik"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cik", "accn"),
    )


def downgrade() -> None:
    op.drop_table("sec_filing_reads")
