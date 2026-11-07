"""company_yahoo_profiles

Revision ID: f1a3c5e7b9d2
Revises: e8c2a4f6b1d3
Create Date: 2026-11-07 16:00:00.000000

The Company tab's overview for every company it lists (docs/company-financials.md,
*The overview*): Yahoo's company-level figures for the companies nobody holds, keyed by
CIK and refreshed a chunk a day (`company_profile_sync`), and the business summary the
held pass already reads from `.info`, now kept on `fundamental_metrics`. A new table and
a nullable column; nothing existing is rewritten.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a3c5e7b9d2'
down_revision: Union[str, None] = 'e8c2a4f6b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'company_yahoo_profiles',
        sa.Column('cik', sa.Integer(), nullable=False),
        sa.Column('trailing_pe', sa.Float(), nullable=True),
        sa.Column('forward_pe', sa.Float(), nullable=True),
        sa.Column('peg_ratio', sa.Float(), nullable=True),
        sa.Column('price_to_sales', sa.Float(), nullable=True),
        sa.Column('price_to_book', sa.Float(), nullable=True),
        sa.Column('revenue_growth', sa.Float(), nullable=True),
        sa.Column('earnings_growth', sa.Float(), nullable=True),
        sa.Column('fwd_revenue_growth', sa.Float(), nullable=True),
        sa.Column('fwd_eps_growth', sa.Float(), nullable=True),
        sa.Column('profit_margins', sa.Float(), nullable=True),
        sa.Column('gross_margins', sa.Float(), nullable=True),
        sa.Column('operating_margins', sa.Float(), nullable=True),
        sa.Column('market_cap', sa.BigInteger(), nullable=True),
        sa.Column('number_of_analysts', sa.Integer(), nullable=True),
        sa.Column('target_mean_price', sa.Float(), nullable=True),
        sa.Column('target_high_price', sa.Float(), nullable=True),
        sa.Column('target_low_price', sa.Float(), nullable=True),
        sa.Column('quote_type', sa.String(length=20), nullable=True),
        sa.Column('data_currency', sa.String(length=10), nullable=True),
        sa.Column('business_summary', sa.Text(), nullable=True),
        sa.Column('strong_buy', sa.Integer(), nullable=True),
        sa.Column('buy', sa.Integer(), nullable=True),
        sa.Column('hold', sa.Integer(), nullable=True),
        sa.Column('sell', sa.Integer(), nullable=True),
        sa.Column('strong_sell', sa.Integer(), nullable=True),
        sa.Column('next_earnings_date', sa.DateTime(), nullable=True),
        sa.Column('next_eps_estimate', sa.Float(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.Column('miss_count', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['cik'], ['sec_companies.cik'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('cik'),
    )
    op.add_column('fundamental_metrics', sa.Column('business_summary', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('fundamental_metrics') as batch:
        batch.drop_column('business_summary')
    op.drop_table('company_yahoo_profiles')
