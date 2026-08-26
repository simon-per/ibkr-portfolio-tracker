"""add_cash_balances

IBKR's own end-of-day cash balance, so the app can stop valuing an account at the market
value of its holdings alone.

The motivating failure, read off production on 2026-08-26: a rotation sold 25,136 CHF of
positions on 08-21 and redeployed 12,682 of them on 08-24. Holdings fell 68,342 -> 43,631
and recovered to 56,161, so the value chart drew a 36% cliff and a partial recovery for a
period in which the account lost nothing — the money was in cash, which nothing tracked.
The headline Market Value card understated the account by the whole idle balance, about
18% of it, and every "% of portfolio" weight divided by that short total.

`CashService` derives a balance from the trade, deposit and dividend ledgers and needs no
new table to do it — it agreed with an independent derivation from the timeline's own
`external_flow_eur` to about a tenth of a percent. **This table is the measured answer
that corrects it.** The derivation is built from records of things that moved, so it
structurally cannot see broker interest, account fees, or the spread on an FX conversion;
those accumulate in one direction, and on this account they had already put the derived
balance ~250 CHF below zero while it was otherwise fully deployed.

It stays **empty until the `<EquitySummaryInBase>` section is enabled in the Flex portal**,
and that is a supported state rather than a pending migration: `CashService` falls back to
the derived series per day and `cash_source` on every surface says which is being shown.
So this upgrade is inert on an account that never enables it.

`report_date` is unique because the Flex window re-delivers the same days on every sync and
a later statement's figure for a day supersedes an earlier one — the same last-write-wins
reasoning as `PROVISIONAL_PRICE_DAYS` on `market_prices`.

`currency` is stored rather than assumed. The section reports in **IBKR's** account base,
which need not be `app_settings.base_currency`, and stamping a figure with a currency it
has not earned is exactly how SBI was carried 61% high.

Revision ID: s2b9d6e3f7a8
Revises: r1a8c5d2e6f7
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 's2b9d6e3f7a8'
down_revision: Union[str, None] = 'r1a8c5d2e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cash_balances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('cash', sa.Numeric(18, 6), nullable=False),
        sa.Column('stock', sa.Numeric(18, 6), nullable=True),
        sa.Column('total', sa.Numeric(18, 6), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_cash_balances_id'), 'cash_balances', ['id'])
    op.create_index(
        op.f('ix_cash_balances_report_date'), 'cash_balances', ['report_date'], unique=True
    )


def downgrade() -> None:
    # Lossless in the sense that matters: every row is reproducible by re-ingesting a
    # statement, and the cash figure falls back to the derived series meanwhile.
    op.drop_index(op.f('ix_cash_balances_report_date'), table_name='cash_balances')
    op.drop_index(op.f('ix_cash_balances_id'), table_name='cash_balances')
    op.drop_table('cash_balances')
