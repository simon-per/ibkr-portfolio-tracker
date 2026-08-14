"""add_etf_lookthrough_tables

Three tables behind the company-level look-through view.

`isin_identities` caches ISIN -> company identity from two keyless providers. They are
stored side by side with SEPARATE checked-at stamps because they fail independently and in
both directions: GLEIF has no ISIN record at all for the Taiwanese and Korean ordinaries
this account holds (TSMC, Samsung, SK Hynix, Credo) while OpenFIGI resolves every one, and
GLEIF resolves ETF ISINs that are not equities. A NULL identifier beside a non-NULL
checked-at means "asked, that provider has nothing" — distinct from "never asked", which is
the absence of a row. Without that split every run re-asks the same unresolvable ISINs
forever.

`etf_baskets` holds one row per fund describing how good its stored basket is, and
`etf_holdings` the constituent rows. They are split because a `SUM(weight_pct)` over the
holdings cannot distinguish "98.04 because the issuer file carries cash and futures rows"
— EMIM's real number — from "98.04 because 2% of the rows failed to parse".

Note `uix_etf_holding_fund_line` keys on the source file's row index, NOT on
`constituent_isin`: the ISIN has to be nullable (some issuers publish tickers or CUSIPs
only) and SQLite treats NULLs as distinct in a unique index, so an ISIN-keyed constraint
would silently permit duplicate NULL rows while appearing to prevent them.

Revision ID: q0f7b4c1d5e6
Revises: p9e6a3b0c4d5
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'q0f7b4c1d5e6'
down_revision: Union[str, None] = 'p9e6a3b0c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'isin_identities',
        sa.Column('isin', sa.String(12), nullable=False),
        # --- GLEIF: folds share classes (both Alphabet ISINs, both ASML ISINs) ---
        sa.Column('lei', sa.String(20), nullable=True),
        sa.Column('issuer_name', sa.String(200), nullable=True),
        sa.Column('lei_source', sa.String(30), nullable=True),  # gleif_api | gleif_bulk
        sa.Column('lei_checked_at', sa.DateTime(), nullable=True),
        # --- OpenFIGI: folds one share class across venues ---
        sa.Column('share_class_figi', sa.String(12), nullable=True),
        # Stored for diagnosis only and never an input to grouping: compositeFIGI is
        # per-exchange-composite, so unioning on it would merge unrelated lines.
        sa.Column('composite_figi', sa.String(12), nullable=True),
        sa.Column('figi_name', sa.String(200), nullable=True),
        sa.Column('figi_source', sa.String(30), nullable=True),
        sa.Column('figi_checked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=True,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('isin'),
    )
    op.create_index('ix_isin_identities_lei', 'isin_identities', ['lei'])
    op.create_index(
        'ix_isin_identities_share_class_figi', 'isin_identities', ['share_class_figi']
    )

    op.create_table(
        'etf_baskets',
        sa.Column('fund_isin', sa.String(12), nullable=False),
        # The issuer's own as-of where it publishes one; the fetch date where it does not
        # (Xtrackers publishes none at all), with the flag below saying which.
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('as_of_is_issuer_stated', sa.Boolean(), nullable=False,
                  server_default=sa.text('1')),
        sa.Column('source', sa.String(50), nullable=False),
        sa.Column('adapter', sa.String(30), nullable=False),
        # source_rows vs stored_rows vs skipped_rows is what tells a genuine cash
        # allocation apart from a partial parse.
        sa.Column('source_rows', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('stored_rows', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('skipped_rows', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('total_weight_pct', sa.Numeric(9, 6), nullable=False),
        sa.Column('equity_weight_pct', sa.Numeric(9, 6), nullable=False),
        sa.Column('identifier_coverage_pct', sa.Numeric(9, 6), nullable=False),
        # False for issuers whose export has no asset-class column, so the equity
        # whitelist could not be applied.
        sa.Column('asset_class_available', sa.Boolean(), nullable=False,
                  server_default=sa.text('1')),
        sa.Column('fetched_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('fund_isin'),
    )

    op.create_table(
        'etf_holdings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('fund_isin', sa.String(12), nullable=False),
        sa.Column('line_no', sa.Integer(), nullable=False),
        sa.Column('constituent_isin', sa.String(12), nullable=True),
        sa.Column('constituent_ticker', sa.String(32), nullable=True),
        sa.Column('constituent_name', sa.String(200), nullable=False),
        # A PERCENT, matching etf_mappings.py. Xtrackers ships fractions summing to 1.0
        # and iShares percents summing to ~100; normalising at the adapter boundary is
        # mandatory, because a fraction here makes the fund contribute 1/100th of its
        # value and read as 99% cash.
        sa.Column('weight_pct', sa.Numeric(9, 6), nullable=False),
        sa.Column('asset_class', sa.String(40), nullable=True),
        # Stored and deliberately unserved — see app/etf_mappings.py for why the three
        # allocation charts must not read these yet.
        sa.Column('sector', sa.String(100), nullable=True),
        sa.Column('country', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('fund_isin', 'line_no', name='uix_etf_holding_fund_line'),
    )
    op.create_index('ix_etf_holdings_id', 'etf_holdings', ['id'])
    op.create_index('ix_etf_holdings_fund_isin', 'etf_holdings', ['fund_isin'])
    op.create_index(
        'ix_etf_holdings_constituent_isin', 'etf_holdings', ['constituent_isin']
    )


def downgrade() -> None:
    op.drop_index('ix_etf_holdings_constituent_isin', table_name='etf_holdings')
    op.drop_index('ix_etf_holdings_fund_isin', table_name='etf_holdings')
    op.drop_index('ix_etf_holdings_id', table_name='etf_holdings')
    op.drop_table('etf_holdings')

    op.drop_table('etf_baskets')

    op.drop_index('ix_isin_identities_share_class_figi', table_name='isin_identities')
    op.drop_index('ix_isin_identities_lei', table_name='isin_identities')
    op.drop_table('isin_identities')
