"""add_security_logos

Revision ID: a7d3c9e5f1b2
Revises: a2e8c4f6b0d3
Create Date: 2026-10-17 20:00:00.000000

Holding logos, one row per ISIN (GitHub issue #12, docs/logos.md). Empty on arrival: the
first `fetch_logos` run or the next 05:00 pass fills it, and until then every avatar is
the monogram it was before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3c9e5f1b2'
down_revision: Union[str, None] = 'a2e8c4f6b0d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'security_logos',
        sa.Column('isin', sa.String(length=12), nullable=False),
        sa.Column('image', sa.LargeBinary(), nullable=True),
        sa.Column('content_type', sa.String(length=32), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('isin'),
    )


def downgrade() -> None:
    op.drop_table('security_logos')
