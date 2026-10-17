"""
The one way a CLI turns `SYMBOL [EXCHANGE]`, or an id, into a security.

Three CLIs resolved a security themselves. `import_prices` and `purge_dividend_estimates`
refused an ambiguous match; `manage_mappings` took `.first()`. `securities` is unique on
(isin, exchange), not (symbol, exchange), so two rows can answer one symbol@exchange — an
ISIN change keeps the ticker — and there `.first()` checked a mapping's currency against
whichever row SQLite returned first, and `disable --purge-prices` could delete the other
security's prices. One resolver, one rule: an explicit id wins, and ambiguity is refused,
naming every candidate so the caller can pass the id. `tests/test_cli_security_lookup.py`
fails any CLI that queries `Security.symbol` itself.
"""
from typing import List, Optional, Sequence, Type

from sqlalchemy import select

from app.models.security import Security


async def matching_securities(db, symbol: str, exchange: Optional[str] = None) -> List[Security]:
    """Every security answering `symbol` (on `exchange`, when given), in id order."""
    stmt = select(Security).where(Security.symbol == symbol)
    if exchange:
        stmt = stmt.where(Security.exchange == exchange)
    return list((await db.execute(stmt.order_by(Security.id))).scalars().all())


def ambiguity(symbol: str, matches: Sequence[Security]) -> str:
    """The refusal text for an ambiguous match, naming each candidate's id."""
    found = ", ".join(f"id={s.id} {s.symbol}@{s.exchange}" for s in matches)
    return f"symbol={symbol!r} matches {len(matches)} securities: {found}"


async def resolve_security(
    db,
    symbol: Optional[str],
    exchange: Optional[str],
    security_id: Optional[int],
    error: Type[Exception],
) -> Security:
    """
    Exactly one security, or `error` saying why not. An explicit id wins; otherwise the
    symbol (and exchange, when given) must match one row. The caller checks that it was
    given a symbol or an id at all, since each CLI words that in its own arguments.
    """
    if security_id:
        security = (await db.execute(
            select(Security).where(Security.id == int(security_id))
        )).scalar_one_or_none()
        if not security:
            raise error(f"No security with id={security_id}")
        return security

    matches = await matching_securities(db, symbol, exchange)
    if not matches:
        raise error(f"No security matches symbol={symbol!r} exchange={exchange!r}")
    if len(matches) > 1:
        # The same stock on two exchanges is two rows by design (ASML on NASDAQ and
        # AEB), so an ambiguous match must be resolved by the caller, not guessed.
        raise error(ambiguity(symbol, matches))
    return matches[0]
