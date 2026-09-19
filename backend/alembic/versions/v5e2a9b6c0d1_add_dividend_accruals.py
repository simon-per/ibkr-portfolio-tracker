"""add_dividend_accruals

Revision ID: v5e2a9b6c0d1
Revises: u4d1f8a5b9c0
Create Date: 2026-09-19 12:00:00.000000

Holds the dividends IBKR has announced and not yet paid, from the Flex
`<OpenDividendAccruals>` section — the only record anywhere that carries an ex-date and
a pay date for the same payment.

Additive and empty on arrival: the section is off by default in the Flex portal, the
ingest runs unconditionally, and the forecast falls back to a pay date measured from
history until rows appear. Nothing reads this table except the dividend calendar, which
is the point — see the model docstring for why an accrual must never become a third
`source` on `dividend_payments`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v5e2a9b6c0d1'
down_revision: Union[str, None] = 'u4d1f8a5b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dividend_accruals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('security_id', sa.Integer(), nullable=False),
        sa.Column('ex_date', sa.Date(), nullable=True),
        sa.Column('pay_date', sa.Date(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('quantity', sa.Numeric(18, 6), nullable=True),
        sa.Column('gross_amount_eur', sa.Numeric(18, 6), nullable=True),
        sa.Column('withholding_tax_eur', sa.Numeric(18, 6), nullable=True),
        sa.Column('net_amount_eur', sa.Numeric(18, 6), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['security_id'], ['securities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'security_id', 'pay_date', name='uix_dividend_accrual_security_pay'
        ),
    )
    op.create_index(op.f('ix_dividend_accruals_id'), 'dividend_accruals', ['id'])
    op.create_index(
        op.f('ix_dividend_accruals_security_id'), 'dividend_accruals', ['security_id']
    )
    op.create_index(
        op.f('ix_dividend_accruals_pay_date'), 'dividend_accruals', ['pay_date']
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_dividend_accruals_pay_date'), table_name='dividend_accruals')
    op.drop_index(op.f('ix_dividend_accruals_security_id'), table_name='dividend_accruals')
    op.drop_index(op.f('ix_dividend_accruals_id'), table_name='dividend_accruals')
    op.drop_table('dividend_accruals')
