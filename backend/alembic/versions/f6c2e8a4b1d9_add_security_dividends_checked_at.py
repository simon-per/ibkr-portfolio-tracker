"""add_security_dividends_checked_at

Revision ID: f6c2e8a4b1d9
Revises: e5b1d7f3a9c2
Create Date: 2026-10-17 18:00:00.000000

When the dividend pass last had Yahoo's answer for a security, so it asks at most once a
week (GitHub issue #6). NULL on arrival: the first pass after the deploy asks every held
security once, which is what every pass did before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6c2e8a4b1d9'
down_revision: Union[str, None] = 'e5b1d7f3a9c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column('securities', sa.Column('dividends_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('securities') as batch:
        batch.drop_column('dividends_checked_at')
