"""
Which account a row belongs to.

The transaction tables (`securities`, `taxlots`, `trades`, `cash_flows`) were always
broker-agnostic in shape and IBKR-only in fact: `securities.conid` is an IBKR
identifier and `reconcile_taxlots` deletes every open lot it does not find in the
statement it is holding. Adding a second account is therefore not a matter of
inserting rows — it is a matter of giving the IBKR sync a blast radius that matches
its authority.

`account` is a free-form ``String(16)`` rather than an enum, the same judgement
``cash_flows.flow_type`` already makes: a second finpension portfolio, or a pillar-2
vested-benefits account, then needs no migration. The constants below are the ones
the code reasons about; an unknown value is a legal row that simply nothing special
is done with.

**The default is `IBKR` everywhere**, so every row written before this existed — and
every row written by code that has never heard of accounts — reads correctly.
"""

# The brokerage account the app was built around. The column default, and the only
# account the IBKR Flex sync is allowed to reconcile.
IBKR = "ibkr"

# Swiss Pillar 3a, ingested from a finpension transaction export. The CLI takes
# ``--account`` so a second 3a portfolio can use its own label; this is the default.
PILLAR3A = "pillar3a"

# Accounts whose assets are exempt from Swiss wealth tax and whose income is not
# taxable — so they must not reach the Steuerwert, the DA-1 reclaim or the realized
# gains section. Their *contributions* are reported instead, being deductible.
#
# A prefix test rather than a set membership, so `pillar3a-portfolio2` inherits the
# treatment. Getting this wrong produces a wrong tax return, which is why it is a
# rule in one place rather than a literal at three call sites.
TAX_EXEMPT_ACCOUNT_PREFIX = "pillar3a"


def is_tax_exempt(account: str | None) -> bool:
    """True for an account whose holdings and income are outside the Swiss tax base."""
    return bool(account) and account.startswith(TAX_EXEMPT_ACCOUNT_PREFIX)


async def tax_exempt_accounts(db) -> list[str]:
    """
    Every account label in the database whose assets are outside the Swiss tax base.

    Resolved from the data rather than from a constant, so a second 3a portfolio
    ingested under its own label (`--account pillar3a-2`) inherits the treatment
    without anyone editing this file, and an IBKR-only database gets an empty list
    rather than a filter over a name nothing uses.

    Every caller guards on truthiness before building a ``NOT IN``, because an
    empty one is a clause with nothing to say and the failure direction here is a
    wrong tax return.
    """
    from sqlalchemy import select

    from app.models.security import Security
    from app.models.trade import Trade

    labels = set()
    for column in (Security.account, Trade.account):
        rows = await db.execute(select(column).distinct())
        labels.update(a for a in rows.scalars().all() if is_tax_exempt(a))

    return sorted(labels)
