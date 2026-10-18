"""add_dividend_withholding_country

Revision ID: c9f6e1a3b5d7
Revises: c9f6e3a1b5d8
Create Date: 2026-10-18 12:00:00.000000

The country IBKR's withholding cash line names on each `ibkr` dividend row, so the tax
report's DA-1 table keys on the country that actually withheld rather than on the ISIN's
(GitHub issue #62). Additive and nullable: existing rows fill in when a statement covering
them is re-ingested (`app.cli.ingest_flex_xml`, no token spend), and a row that never is
falls back to the ISIN prefix. See the model comment.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f6e1a3b5d7'
down_revision: Union[str, None] = 'c9f6e3a1b5d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column('dividend_payments',
                  sa.Column('withholding_country', sa.String(2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('dividend_payments') as batch:
        batch.drop_column('withholding_country')
