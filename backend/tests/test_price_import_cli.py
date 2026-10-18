"""
Tests for the offline price import (`app/cli/import_prices.py`).

This path exists because the automatic feed can be wrong or simply unavailable: SBI@TSE
had to have 529 poisoned rows deleted, which left the position valued at 0.00 until the
next scheduled Yahoo job — and Yahoo could not be called on demand to fix it.

The thing worth pinning is that it cannot reintroduce the bug it repairs. The original
failure was a *currency* one: a US fund's USD closes were written under a CAD security,
and because nothing downstream compares the two, the position was carried 61% high for
months. So a mismatched file is refused whole, and a malformed one is refused whole too —
a partially-applied price series is indistinguishable afterwards from a complete one.

Offline: no IBKR, no Yahoo, no network of any kind.
"""
import json
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401  register all mappers
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.sync_run import SyncRun
from app.cli import import_prices as cli
from app.cli.import_prices import PriceImportError, parse_payload
from tests.memory_db import memory_engine

SBI_PAYLOAD = {
    "symbol": "SBI",
    "exchange": "TSE",
    "currency": "CAD",
    "source": "ibkr",
    "prices": [
        {"date": "2026-07-24", "close": 4.85},
        {"date": "2026-07-27", "close": 4.79},
    ],
}


@pytest_asyncio.fixture
async def db(monkeypatch):
    """In-memory DB, wired in as the CLI's session factory so it needs no patching."""
    engine = await memory_engine()

    session = AsyncSession(engine, expire_on_commit=False)
    session.add_all([
        Security(id=1, isin="GB00BG5NDX91", symbol="SBI", description="SERABI GOLD PLC",
                 currency="CAD", conid=322447809, asset_category="STK", exchange="TSE"),
        Security(id=2, isin="NL0010273215", symbol="ASML", description="ASML HOLDING",
                 currency="USD", conid=117902840, asset_category="STK", exchange="NASDAQ"),
        Security(id=3, isin="NL0010273215", symbol="ASML", description="ASML HOLDING",
                 currency="EUR", conid=117589399, asset_category="STK", exchange="AEB"),
    ])
    await session.commit()

    # The CLI opens its own session; hand it this one and neutralise the close so the
    # test can still read the result afterwards.
    class _Factory:
        def __call__(self):
            return _Ctx()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "AsyncSessionLocal", _Factory())
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


def write(tmp_path, payload, name="prices.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def _prices(db, security_id=1):
    result = await db.execute(
        select(MarketPrice).where(MarketPrice.security_id == security_id)
        .order_by(MarketPrice.date)
    )
    return list(result.scalars().all())


async def _runs(db):
    result = await db.execute(select(SyncRun))
    return list(result.scalars().all())


# --- validation, before anything touches the database --------------------------------


def test_payload_rows_are_normalised_and_sorted():
    target, rows = parse_payload(SBI_PAYLOAD)

    assert target["currency"] == "CAD"
    assert target["source"] == "ibkr"
    assert [r["date"] for r in rows] == [date(2026, 7, 24), date(2026, 7, 27)]
    assert rows[0]["close_price"] == Decimal("4.85")


def test_a_repeated_date_resolves_deterministically_to_the_last_row():
    """Otherwise the winner depends on upsert ordering, which is not something a
    reader of the file could predict."""
    _, rows = parse_payload({
        **SBI_PAYLOAD,
        "prices": [{"date": "2026-07-27", "close": 1.0}, {"date": "2026-07-27", "close": 4.79}],
    })

    assert len(rows) == 1
    assert rows[0]["close_price"] == Decimal("4.79")


@pytest.mark.parametrize("prices", [
    [],                                          # nothing to import
    [{"date": "2026-07-27"}],                    # no close
    [{"date": "27/07/2026", "close": 4.79}],     # ambiguous date format
    [{"date": "2026-07-27", "close": 0}],        # a zero price would read as a crash
    [{"date": "2026-07-27", "close": -4.79}],
], ids=["empty", "no-close", "bad-date", "zero", "negative"])
def test_a_malformed_file_is_refused_whole(prices):
    with pytest.raises(PriceImportError):
        parse_payload({**SBI_PAYLOAD, "prices": prices})


def test_a_currency_that_is_not_a_code_is_refused():
    with pytest.raises(PriceImportError):
        parse_payload({**SBI_PAYLOAD, "currency": "Canadian dollars"})


# --- the import itself ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_writes_the_prices_and_records_the_run(db, tmp_path):
    code = await cli.import_prices(write(tmp_path, SBI_PAYLOAD))

    assert code == 0
    prices = await _prices(db)
    assert [p.close_price for p in prices] == [Decimal("4.85"), Decimal("4.79")]
    assert {p.currency for p in prices} == {"CAD"}
    assert {p.source for p in prices} == {"ibkr"}

    runs = await _runs(db)
    assert [r.sync_type for r in runs] == ["manual_prices"]
    assert runs[0].status == "success"
    assert runs[0].details["prices_imported"] == 2


@pytest.mark.asyncio
async def test_a_currency_mismatch_aborts_and_writes_nothing(db, tmp_path):
    """The SBI regression in one line: USD closes under a CAD security. Refusing is the
    whole point — a mislabelled currency is invisible to everything downstream."""
    path = write(tmp_path, {**SBI_PAYLOAD, "currency": "USD"})

    code = await cli.import_prices(path)

    assert code == 1
    assert await _prices(db) == []
    runs = await _runs(db)
    assert [r.status for r in runs] == ["error"]
    assert "CAD" in runs[0].message


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db, tmp_path):
    code = await cli.import_prices(write(tmp_path, SBI_PAYLOAD), dry_run=True)

    assert code == 0
    assert await _prices(db) == []
    assert await _runs(db) == []


@pytest.mark.asyncio
async def test_re_running_upserts_instead_of_duplicating(db, tmp_path):
    """Same guarantee as the XML ingest: safe to re-run, and a later Yahoo fetch can
    overwrite a row cleanly rather than colliding with it."""
    await cli.import_prices(write(tmp_path, SBI_PAYLOAD))
    revised = {**SBI_PAYLOAD, "prices": [{"date": "2026-07-27", "close": 4.78}]}

    await cli.import_prices(write(tmp_path, revised, name="revised.json"))

    prices = await _prices(db)
    assert len(prices) == 2
    assert prices[-1].close_price == Decimal("4.78")


@pytest.mark.asyncio
async def test_an_ambiguous_symbol_is_refused_rather_than_guessed(db, tmp_path):
    """ASML is two securities by design (NASDAQ and AEB, different currencies). Picking
    one would silently price the other."""
    path = write(tmp_path, {
        "symbol": "ASML", "currency": "USD", "source": "ibkr",
        "prices": [{"date": "2026-07-27", "close": 1629.44}],
    })

    code = await cli.import_prices(path)

    assert code == 1
    assert await _prices(db, security_id=2) == []
    assert "matches 2 securities" in (await _runs(db))[0].message


@pytest.mark.asyncio
async def test_the_exchange_disambiguates_that_same_symbol(db, tmp_path):
    path = write(tmp_path, {
        "symbol": "ASML", "exchange": "AEB", "currency": "EUR", "source": "ibkr",
        "prices": [{"date": "2026-07-27", "close": 1435.60}],
    })

    assert await cli.import_prices(path) == 0
    assert len(await _prices(db, security_id=3)) == 1


@pytest.mark.asyncio
async def test_security_id_override_wins_over_the_file(db, tmp_path):
    path = write(tmp_path, {**SBI_PAYLOAD, "symbol": "NOPE", "exchange": "NOWHERE"})

    assert await cli.import_prices(path, security_id=1) == 0
    assert len(await _prices(db)) == 2


@pytest.mark.asyncio
async def test_an_unknown_security_writes_nothing(db, tmp_path):
    path = write(tmp_path, {**SBI_PAYLOAD, "symbol": "GHOST", "exchange": "NOWHERE"})

    assert await cli.import_prices(path) == 1
    assert await _prices(db) == []
