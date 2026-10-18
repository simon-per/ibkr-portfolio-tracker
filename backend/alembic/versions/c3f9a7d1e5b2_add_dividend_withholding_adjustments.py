"""add_dividend_withholding_adjustments

Revision ID: c3f9a7d1e5b2
Revises: c9f6e1a3b5d7
Create Date: 2026-10-18 12:00:00.000000

Withholding lines IBKR posts on a day with no dividend of the same security (refunds and
corrections of an earlier dividend's tax), recorded once each so folding them into that
dividend is idempotent across overlapping Flex statements (GitHub issue #60). Additive
and empty on arrival: the next statement that carries such a line records and applies
it. See the model docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f9a7d1e5b2'
down_revision: Union[str, None] = 'c9f6e1a3b5d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dividend_withholding_adjustments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ib_key', sa.String(64), nullable=False),
        sa.Column('security_id', sa.Integer(), nullable=False),
        sa.Column('posted_date', sa.Date(), nullable=False),
        sa.Column('line_type', sa.String(16), nullable=False, server_default='WHTAX'),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('amount', sa.Numeric(18, 6), nullable=False),
        sa.Column('withholding_eur', sa.Numeric(18, 6), nullable=False),
        sa.Column('target_pay_date', sa.Date(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['security_id'], ['securities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_dividend_withholding_adjustments_id'),
        'dividend_withholding_adjustments', ['id'],
    )
    op.create_index(
        op.f('ix_dividend_withholding_adjustments_ib_key'),
        'dividend_withholding_adjustments', ['ib_key'], unique=True,
    )
    op.create_index(
        op.f('ix_dividend_withholding_adjustments_security_id'),
        'dividend_withholding_adjustments', ['security_id'],
    )
    op.create_index(
        'ix_dividend_wht_adjustment_target',
        'dividend_withholding_adjustments', ['security_id', 'target_pay_date'],
    )


def downgrade() -> None:
    op.drop_index('ix_dividend_wht_adjustment_target',
                  table_name='dividend_withholding_adjustments')
    op.drop_index(op.f('ix_dividend_withholding_adjustments_security_id'),
                  table_name='dividend_withholding_adjustments')
    op.drop_index(op.f('ix_dividend_withholding_adjustments_ib_key'),
                  table_name='dividend_withholding_adjustments')
    op.drop_index(op.f('ix_dividend_withholding_adjustments_id'),
                  table_name='dividend_withholding_adjustments')
    op.drop_table('dividend_withholding_adjustments')
