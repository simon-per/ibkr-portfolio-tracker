"""
Every CLI resolves a security the same way (`app/cli/security_lookup.py`).

Three CLIs had their own lookup; two refused an ambiguous `symbol@exchange` and
`manage_mappings` took `.first()`. `securities` is unique on (isin, exchange), so two rows
can share a symbol on one exchange, and there `disable --purge-prices` could delete the
wrong security's prices. The family test is the source scan: no CLI but the shared module
may query `Security.symbol` itself. The rest pins the rule on the mapping CLI, the one
that guessed.

Offline: no Yahoo, no IBKR, no network.
"""
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401  register all mappers
from app.cli import manage_mappings as cli
from app.cli.manage_mappings import build_parser
from app.cli.security_lookup import resolve_security
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.ticker_mapping import TickerMapping
from tests.memory_db import memory_engine

CLI_DIR = Path(__file__).resolve().parents[1] / "app" / "cli"


def test_no_cli_but_the_shared_module_looks_a_security_up_by_symbol():
    offenders = []
    for path in sorted(CLI_DIR.glob("*.py")):
        if path.name == "security_lookup.py":
            continue
        code = "\n".join(
            line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
        )
        if re.search(r"\bSecurity\.symbol\b", code):
            offenders.append(path.name)
    assert offenders == [], (
        f"{offenders} query Security.symbol directly — resolve through "
        f"app/cli/security_lookup.py so an ambiguous symbol is refused, not guessed"
    )


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    # One ticker on one exchange, two ISINs: the shape an ISIN change leaves behind.
    session.add_all([
        Security(id=1, isin="CA0000000001", symbol="OLD", description="OLD CO",
                 currency="CAD", conid=1001, asset_category="STK", exchange="TSE"),
        Security(id=2, isin="CA0000000002", symbol="OLD", description="OLD CO (NEW ISIN)",
                 currency="CAD", conid=1002, asset_category="STK", exchange="TSE"),
    ])
    for sid in (1, 2):
        session.add(MarketPrice(security_id=sid, date=date(2026, 7, 24),
                                close_price=Decimal("7.7"), currency="CAD",
                                source="yahoo_finance"))
    await session.commit()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _Ctx())
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


def args(*argv):
    return build_parser().parse_args(list(argv))


async def _price_rows(db):
    return list((await db.execute(select(MarketPrice))).scalars().all())


@pytest.mark.asyncio
async def test_set_refuses_an_ambiguous_symbol_and_writes_nothing(db, capsys):
    assert await cli.run(args("set", "OLD", "TSE", "OLD.TO")) == 1
    err = capsys.readouterr().err
    assert "matches 2 securities: id=1 OLD@TSE, id=2 OLD@TSE" in err
    assert (await db.execute(select(TickerMapping))).scalars().all() == []


@pytest.mark.asyncio
async def test_disable_with_purge_refuses_rather_than_purging_one_of_two(db):
    db.add(TickerMapping(ibkr_symbol="OLD", ibkr_exchange="TSE", yahoo_ticker="OLD.V",
                         source="manual", is_active=True))
    await db.commit()

    assert await cli.run(args("disable", "OLD", "TSE", "--purge-prices")) == 1
    assert len(await _price_rows(db)) == 2
    mapping = (await db.execute(select(TickerMapping))).scalars().one()
    assert mapping.is_active


@pytest.mark.asyncio
async def test_list_flags_the_ambiguity_instead_of_reading_one_row(db, capsys):
    db.add(TickerMapping(ibkr_symbol="OLD", ibkr_exchange="TSE", yahoo_ticker="OLD.TO",
                         source="manual", is_active=True))
    await db.commit()

    assert await cli.run(args("list")) == 0
    assert "AMBIGUOUS: 2 securities" in capsys.readouterr().out


class _Refused(Exception):
    pass


@pytest.mark.asyncio
async def test_the_resolver_lets_an_explicit_id_settle_it(db):
    assert (await resolve_security(db, "OLD", "TSE", 2, _Refused)).id == 2
    with pytest.raises(_Refused, match="matches 2 securities"):
        await resolve_security(db, "OLD", "TSE", None, _Refused)
    with pytest.raises(_Refused, match="No security matches"):
        await resolve_security(db, "NOPE", "TSE", None, _Refused)
    with pytest.raises(_Refused, match="No security with id=99"):
        await resolve_security(db, None, None, 99, _Refused)
