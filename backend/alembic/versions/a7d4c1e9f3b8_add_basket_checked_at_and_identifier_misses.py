"""add_basket_checked_at_and_identifier_misses

Revision ID: a7d4c1e9f3b8
Revises: a7d3c9e5f1b2
Create Date: 2026-10-17 20:00:00.000000

The evening basket refresh fetches daily issuers every day instead of once a week
(GitHub #8), and three stores keep that from costing more than it has to:

- `etf_baskets.checked_at`: when the issuer was last asked, whatever the answer, so a
  fund is asked at most once per its fetch interval.
- `etf_baskets.content_sha256`: what the last parse produced, so an unchanged re-fetch
  rewrites nothing and triggers no CINS/SEDOL pass.
- `constituent_identifier_misses`: CINS/SEDOL identifiers OpenFIGI had no answer for, so
  a permanent miss is re-asked monthly rather than every pass.

All NULL or empty on arrival: the first evening after the deploy fetches every held
basket once and rewrites it, which is what a refresh did before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d4c1e9f3b8'
down_revision: Union[str, None] = 'a7d3c9e5f1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain ADD COLUMNs: SQLite does them in place, with no table rebuild.
    op.add_column('etf_baskets', sa.Column('checked_at', sa.DateTime(), nullable=True))
    op.add_column('etf_baskets', sa.Column('content_sha256', sa.String(length=64), nullable=True))
    op.create_table(
        'constituent_identifier_misses',
        sa.Column('identifier', sa.String(length=20), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('identifier'),
    )


def downgrade() -> None:
    op.drop_table('constituent_identifier_misses')
    with op.batch_alter_table('etf_baskets') as batch:
        batch.drop_column('content_sha256')
        batch.drop_column('checked_at')
