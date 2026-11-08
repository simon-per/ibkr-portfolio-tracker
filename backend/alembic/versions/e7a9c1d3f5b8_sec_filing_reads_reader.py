"""sec_filing_reads_reader

Revision ID: e7a9c1d3f5b8
Revises: a8d0f2b4c6e7
Create Date: 2026-11-08 22:00:00.000000

What a stored filing read looked for (`edgar_filing_facts.READER`): 1 the dividend per share
alone, 2 the diluted EPS too. BKR tags its EPS only on its Class A since 2023, so companyfacts
holds none of its quarters' EPS after 2022; the reader now reads it from the filings. Every
read stored before is a 1, so a filing an EPS gap names is read once more, and a filing a
dividend gap names never again. Nothing is requested here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7a9c1d3f5b8"
down_revision: Union[str, None] = "a8d0f2b4c6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN with a constant default: SQLite does it in place.
    op.add_column(
        "sec_filing_reads",
        sa.Column("reader", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    with op.batch_alter_table("sec_filing_reads") as batch:
        batch.drop_column("reader")
