"""
Which securities Yahoo may be asked about at all.

Until 2026-09-06 there was **no way to leave a security alone**. Every service that
loops against Yahoo selects `Security` unfiltered -- market data, fundamentals,
analyst ratings, dividends and the allocation sync, seven call sites across six
modules -- and none of them had a predicate to consult. `ticker_mappings.is_active`
looks like the off switch and is not: clearing it only removes tier 1 of the lookup,
after which `_get_yahoo_ticker` falls through to the suffix logic, tries variations,
and can **auto-save a bare-symbol mapping** for whatever unrelated US listing happens
to answer. That is how a Toronto gold miner was priced as a US fund for months.

An instrument Yahoo simply does not have -- a Swiss pillar 3a institutional fund
tranche, sold only inside a pension wrapper and quoted nowhere -- is precisely the
shape that failure needs. Asking is waste at best and a poisoned mapping at worst.

So `securities.price_source` is that switch, and this module is the one place the
question is answered. It is deliberately **not** derived from `securities.account`:
of the two funds this was built for, one is reachable on Yahoo under a Morningstar
fund quote and the other is not, and they sit in the same account.

The rule is stated once for prices and applied to everything else Yahoo serves,
because "Yahoo has no bar for this instrument" and "Yahoo has no `.info`, no ratings
and no dividend history for it" are the same fact. `tests/test_yahoo_eligibility_family.py`
walks the AST of every module importing `yfinance` and fails any that selects
`Security` without reaching this predicate -- the family form, so the eighth service
is caught by construction rather than by somebody noticing.
"""
from sqlalchemy.sql.elements import ColumnElement

from app.models.security import PRICE_SOURCE_YAHOO, Security


def yahoo_eligible() -> ColumnElement[bool]:
    """
    SQLAlchemy criterion: securities Yahoo may be asked about.

    Use as ``select(Security).where(yahoo_eligible())``. Selected through the shared
    predicate rather than filtered in Python for the reason `needs_allocation_refresh`
    is: two loops that answer "which securities are pending" differently is how this
    codebase's rules go to diverge.
    """
    return Security.price_source == PRICE_SOURCE_YAHOO


def is_yahoo_eligible(security: Security) -> bool:
    """
    Row-level form, for the innermost gate.

    `fetch_and_cache_prices` and `sync_security_prices` are reachable directly, not
    only through a loop that has already filtered -- so a rule enforced solely at the
    query is a rule that leaks the first time somebody calls the inner function.
    """
    return getattr(security, "price_source", PRICE_SOURCE_YAHOO) == PRICE_SOURCE_YAHOO
