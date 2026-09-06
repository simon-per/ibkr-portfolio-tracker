"""account_dimension

Give every transaction row an account, so a second one can exist without the IBKR
sync destroying it.

The four transaction tables were always broker-agnostic in *shape* and IBKR-only in
*fact*, and the gap between those two is not cosmetic. `reconcile_taxlots` selects
`get_open_taxlots()` with no filter but `is_open`, unions those security ids into
`all_security_ids`, and deletes. An IBKR statement naturally never mentions a Swiss
Pillar 3a fund, so the first Flex sync after such a row existed would delete its open
lots and then book the position as sold. The empty-statement wipe guard has the mirror
problem: it compares the incoming statement's lots against *every* open lot in the
database, so a foreign account's holdings would suppress the guard that protects the
IBKR ones.

So this revision is a precondition, not a feature. It adds no data and changes no
behaviour on an IBKR-only database: every column defaults to 'ibkr', which is exactly
what every existing row is.

Four changes:

1. `account` on securities / trades / cash_flows / cash_balances. String(16), NOT
   NULL, server_default 'ibkr', indexed. A string rather than an enum for the reason
   `cash_flows.flow_type` is one: a second 3a portfolio, or a pillar-2 vested-benefits
   account, then needs no migration.

2. `securities.conid` becomes nullable. It is an IBKR identifier and a Swisscanto
   fund has none, so there is no honest value for it, and a *fabricated* one that
   looks like a contract id would be the SBI lesson in a new place: a label a row
   has not earned. The UNIQUE index on it is kept:
   SQLite applies uniqueness per non-NULL value, so any number of conid-less
   securities coexist while two IBKR ones still cannot collide.

   `trades.conid` deliberately stays NOT NULL. It is a String(32), so the ISIN is a
   perfectly good durable identifier there, and weakening a contract every IBKR row
   honours -- to avoid writing a value we have -- is a bad trade. It is only ever
   matched, never parsed: `get_by_conid_in_range` is simply never satisfied by a
   `CH...` string, and `persist_transactions` filters through `.isdigit()`.

   The sharp edge is `SecurityRepository.upsert`, which keys on conid. In SQLAlchemy
   `conid == None` renders `IS NULL`, so with a NULL it does not merely fail to find
   a new security -- it *matches an existing conid-less one* and overwrites it with
   another fund's fields, or raises MultipleResultsFound once there are two. So
   `upsert` and `get_by_conid` refuse a NULL outright, and a non-IBKR security
   upserts on (isin, exchange), which is already uniquely constrained.

3. `securities.price_source`. Orthogonal to `account`, deliberately: of the two funds
   this was built for, one is reachable on Yahoo and one is not. There is currently no
   way to tell the price loop to leave a security alone. It takes `get_all()`
   unfiltered, `ticker_mappings.is_active=False` does not stop a fetch, and the
   variation loop can auto-save a *bare symbol* mapping, which is how a Toronto gold
   miner came to be priced as a US fund for months. 'manual' is that opt-out.

4. `cash_balances`: the unique key moves from `report_date` to
   `(account, report_date)`. A measured balance is a level *of something*; two accounts
   reporting a figure for one date is not a conflict, and letting one overwrite the
   other would splice a foreign correction into `CashService._apply_measured`, whose
   arithmetic is `level - derived_running`.

SQLite cannot alter a column's nullability or drop an index-backed uniqueness in
place, so 2 and 4 go through `batch_alter_table`, which rebuilds the table. The
container runs `alembic upgrade head` on every start and auto-deploy backs the DB up
first.

The downgrade refuses before any DDL if non-IBKR rows exist, rather than deleting
them. Without `account` a Pillar 3a lot is indistinguishable from an IBKR one, so the
next Flex sync would delete it as absent from the statement. Losing a column is
recoverable; losing the rows is not.

Revision ID: t3c0e7f4a8b9
Revises: s2b9d6e3f7a8
Create Date: 2026-09-06 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 't3c0e7f4a8b9'
down_revision: Union[str, None] = 's2b9d6e3f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ACCOUNTED = ('securities', 'trades', 'cash_flows', 'cash_balances')


def upgrade() -> None:
    # --- 1. The account column -------------------------------------------
    for table in _ACCOUNTED:
        op.add_column(
            table,
            sa.Column('account', sa.String(16), nullable=False, server_default='ibkr'),
        )
        op.create_index(op.f(f'ix_{table}_account'), table, ['account'])

    # --- 3. Price provenance ---------------------------------------------
    op.add_column(
        'securities',
        sa.Column('price_source', sa.String(16), nullable=False, server_default='yahoo'),
    )

    # --- 2. conid becomes nullable ---------------------------------------
    with op.batch_alter_table('securities') as batch:
        batch.alter_column('conid', existing_type=sa.Integer(), nullable=True)

    # --- 4. Account-scoped cash balance identity -------------------------
    # The old uniqueness rode on the index, so it is the index that has to go.
    op.drop_index('ix_cash_balances_report_date', table_name='cash_balances')
    op.create_index(
        op.f('ix_cash_balances_report_date'), 'cash_balances', ['report_date']
    )
    with op.batch_alter_table('cash_balances') as batch:
        batch.create_unique_constraint(
            'uix_cash_balance_account_date', ['account', 'report_date']
        )


def downgrade() -> None:
    # Checked BEFORE any DDL, deliberately, the same shape as o8d5f2a9b3c4. Raising
    # midway would leave the schema half-downgraded, and here the stakes are higher
    # than a schema: without `account` a Pillar 3a lot is indistinguishable from an
    # IBKR one, so the next Flex sync deletes it as absent from the statement. Refuse
    # while the columns still exist and the rows can still be exported.
    conn = op.get_bind()
    stranded = []
    for table in _ACCOUNTED:
        rows = conn.execute(sa.text(
            "SELECT account, COUNT(*) FROM " + table
            + " WHERE account <> 'ibkr' GROUP BY account"
        )).fetchall()
        stranded += ["{}: {} row(s) on account {!r}".format(table, n, acct)
                     for acct, n in rows]
    if stranded:
        raise RuntimeError(
            "Cannot drop the account dimension while non-IBKR rows exist. The next "
            "IBKR sync would delete them as missing from the statement. Found "
            + "; ".join(stranded)
            + ". Remove those rows first if you really mean to downgrade."
        )

    with op.batch_alter_table('cash_balances') as batch:
        batch.drop_constraint('uix_cash_balance_account_date', type_='unique')
    op.drop_index(op.f('ix_cash_balances_report_date'), table_name='cash_balances')
    op.create_index(
        'ix_cash_balances_report_date', 'cash_balances', ['report_date'], unique=True
    )

    # A NULL conid cannot survive a NOT NULL column. There are none: the guard above
    # already refused every non-IBKR row, and the IBKR ingest always supplies one.
    with op.batch_alter_table('securities') as batch:
        batch.alter_column('conid', existing_type=sa.Integer(), nullable=False)

    op.drop_column('securities', 'price_source')

    for table in reversed(_ACCOUNTED):
        op.drop_index(op.f(f'ix_{table}_account'), table_name=table)
        op.drop_column(table, 'account')
