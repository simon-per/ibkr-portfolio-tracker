"""add_dividend_date_pairs

Revision ID: y8b4d0f2a6c3
Revises: x7a3c9e1f5b2d
Create Date: 2026-10-03 10:00:00.000000

IBKR's own (ex-date, pay date) for each dividend, from the Flex
`<ChangeInDividendAccruals>` section, so the ex→pay lag is read rather than inferred by
proximity. Additive and empty on arrival; an append-only log that grows with each sync.
See the model docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'y8b4d0f2a6c3'
down_revision: Union[str, None] = 'x7a3c9e1f5b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dividend_date_pairs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('security_id', sa.Integer(), nullable=False),
        sa.Column('ex_date', sa.Date(), nullable=False),
        sa.Column('pay_date', sa.Date(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['security_id'], ['securities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'security_id', 'ex_date', 'pay_date', name='uix_dividend_date_pair'
        ),
    )
    op.create_index(op.f('ix_dividend_date_pairs_id'), 'dividend_date_pairs', ['id'])
    op.create_index(
        op.f('ix_dividend_date_pairs_security_id'), 'dividend_date_pairs', ['security_id']
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_dividend_date_pairs_security_id'), table_name='dividend_date_pairs')
    op.drop_index(op.f('ix_dividend_date_pairs_id'), table_name='dividend_date_pairs')
    op.drop_table('dividend_date_pairs')
