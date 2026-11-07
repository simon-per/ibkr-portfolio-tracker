"""add_sec_company_profile

Revision ID: e8c2a4f6b1d3
Revises: d3b7f9a1c5e2
Create Date: 2026-11-07 15:00:00.000000

The Company tab's overview names each company's industry and fiscal year end, from its
EDGAR `submissions` document: the SIC code and its description, the `MMDD` year end, and
when the pass last asked (at most monthly). NULL on arrival; the first Sunday pass fills
them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8c2a4f6b1d3'
down_revision: Union[str, None] = 'd3b7f9a1c5e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMNs: SQLite does them in place, with no table rebuild.
    op.add_column('sec_companies', sa.Column('sic', sa.Integer(), nullable=True))
    op.add_column('sec_companies', sa.Column('sic_description', sa.String(length=200), nullable=True))
    op.add_column('sec_companies', sa.Column('fiscal_year_end', sa.String(length=4), nullable=True))
    op.add_column('sec_companies', sa.Column('submissions_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sec_companies') as batch:
        batch.drop_column('submissions_checked_at')
        batch.drop_column('fiscal_year_end')
        batch.drop_column('sic_description')
        batch.drop_column('sic')
