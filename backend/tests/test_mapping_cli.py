"""
Tests for the ticker-mapping CLI (`app/cli/manage_mappings.py`).

`ticker_mappings` is tier 1 of price resolution, so a wrong row silently redirects a
position's prices to a different company — and because tier 1 wins, it keeps doing so even
after the suffix logic that would have been right is fixed. That is the SBI@TSE failure
(a Toronto CAD holding priced off a US fund, 61% high, for months).

What these tests pin is therefore mostly about refusing and reporting, not about writing:
an explicit currency contradiction is rejected outright, a suffix-less ticker for a foreign
listing is called out, and a disagreement already in the table is surfaced by `list` rather
than waiting to be noticed in the portfolio total.

Offline: no Yahoo, no IBKR, no network at all.
"""
import json
from datetime import date
from decimal import Decimal

from types import SimpleNamespace
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.sync_run import SyncRun
from app.models.ticker_mapping import TickerMapping
from app.cli import manage_mappings as cli
from app.cli.manage_mappings import MappingError, build_parser, implied_currency
from app.repositories.ticker_mapping_repository import TickerMappingRepository
from app.services.market_data_service import MarketDataService


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session = AsyncSession(engine, expire_on_commit=False)
    session.add_all([
        # Serabi Gold: Toronto, CAD — the security the original bug mispriced.
        Security(id=1, isin="GB00BG5NDX91", symbol="SBI", description="SERABI GOLD PLC",
                 currency="CAD", conid=322447809, asset_category="STK", exchange="TSE"),
        # A plain US listing, where a bare Yahoo ticker is correct.
        Security(id=2, isin="US0231351067", symbol="AMZN", description="AMAZON",
                 currency="USD", conid=3691937, asset_category="STK", exchange="NASDAQ"),
    ])
    for day, price in [(date(2026, 7, 24), 7.70), (date(2026, 7, 27), 7.65)]:
        session.add(MarketPrice(security_id=1, date=day, close_price=Decimal(str(price)),
                                currency="CAD", source="yahoo_finance"))
    session.add(MarketPrice(security_id=2, date=date(2026, 7, 24), close_price=Decimal("231"),
                            currency="USD", source="yahoo_finance"))
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


async def _mappings(db, symbol=None):
    stmt = select(TickerMapping)
    if symbol:
        stmt = stmt.where(TickerMapping.ibkr_symbol == symbol)
    return list((await db.execute(stmt)).scalars().all())


async def _prices(db, security_id):
    return list((await db.execute(
        select(MarketPrice).where(MarketPrice.security_id == security_id)
    )).scalars().all())


async def _runs(db):
    return list((await db.execute(select(SyncRun))).scalars().all())


# --- what a ticker implies on its own ------------------------------------------------


def test_implied_currency_reads_suffixes_overrides_and_admits_ignorance():
    service = MarketDataService.__new__(MarketDataService)

    assert implied_currency(service, "SBI.TO") == "CAD"
    assert implied_currency(service, "2330.TW") == "TWD"
    assert implied_currency(service, "005930.KS") == "KRW"
    # A hand-checked override outranks the suffix: SMH.L is a USD ETF listed in London.
    assert implied_currency(service, "SMH.L") == "USD"
    # A bare ticker genuinely implies nothing, and saying "None" is what lets `set`
    # distinguish "contradicts the security" from "cannot tell".
    assert implied_currency(service, "SOXQ") is None


# --- set -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_creates_then_updates_and_always_stamps_manual(db):
    assert await cli.run(args("set", "SBI", "TSE", "SBI.TO", "--notes", "Toronto")) == 0

    rows = await _mappings(db, "SBI")
    assert len(rows) == 1 and rows[0].yahoo_ticker == "SBI.TO"
    assert rows[0].source == "manual"

    assert await cli.run(args("set", "SBI", "TSE", "SBI.V")) == 0

    rows = await _mappings(db, "SBI")
    assert len(rows) == 1                      # updated, not duplicated
    assert rows[0].yahoo_ticker == "SBI.V"
    assert rows[0].source == "manual"


@pytest.mark.asyncio
async def test_a_ticker_contradicting_the_securitys_currency_is_refused(db):
    """SBI.L implies GBP against a CAD security. Refusing matters because the failure is
    invisible downstream: prices do arrive, they are just the wrong company's."""
    assert await cli.run(args("set", "SBI", "TSE", "SBI.L")) == 1

    assert await _mappings(db, "SBI") == []
    runs = await _runs(db)
    assert [r.status for r in runs] == ["error"]
    assert "CAD" in runs[0].message and "GBP" in runs[0].message


@pytest.mark.asyncio
async def test_a_bare_ticker_for_a_foreign_listing_warns_but_is_allowed(db, capsys):
    """Exactly the shape of the original bug — but a bare ticker implies no currency, so it
    cannot be *proved* wrong, and an operator may know better than the suffix map. Warn
    loudly; don't block."""
    assert await cli.run(args("set", "SBI", "TSE", "SBI")) == 0

    out = capsys.readouterr().out
    assert "WARNING" in out and "no suffix" in out
    assert (await _mappings(db, "SBI"))[0].yahoo_ticker == "SBI"


@pytest.mark.asyncio
async def test_a_bare_ticker_for_a_us_listing_is_silent(db, capsys):
    """The common, correct case: NASDAQ has no suffix, so AMZN is right and must not be
    nagged about, or the warning above becomes noise people learn to ignore."""
    assert await cli.run(args("set", "AMZN", "NASDAQ", "AMZN")) == 0

    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_set_stores_a_mapping_for_a_security_that_does_not_exist_yet(db, capsys):
    """Pinning a mapping ahead of the statement that first carries the security is the
    reason this ran tonight for 2330@TWSE — it has to be allowed."""
    assert await cli.run(args("set", "2330", "TWSE", "2330.TW")) == 0

    out = capsys.readouterr().out
    assert "no security matches" in out
    assert (await _mappings(db, "2330"))[0].yahoo_ticker == "2330.TW"


@pytest.mark.asyncio
async def test_set_dry_run_writes_nothing(db):
    assert await cli.run(args("set", "SBI", "TSE", "SBI.TO", "--dry-run")) == 0

    assert await _mappings(db, "SBI") == []
    assert await _runs(db) == []


@pytest.mark.asyncio
async def test_set_records_the_edit_in_sync_runs(db):
    """A mapping change being invisible is why SBI went unnoticed for months."""
    await cli.run(args("set", "SBI", "TSE", "SBI.TO"))

    runs = await _runs(db)
    assert [r.sync_type for r in runs] == ["manual_mapping"]
    assert runs[0].details["action"] == "set"
    assert runs[0].details["yahoo_ticker"] == "SBI.TO"


# --- disable --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_stops_the_lookup_but_keeps_the_row(db):
    await cli.run(args("set", "SBI", "TSE", "SBI.TO"))

    assert await cli.run(args("disable", "SBI", "TSE")) == 0

    # get_mapping filters on is_active, so resolution falls back to the suffix logic...
    assert await TickerMappingRepository(db).get_mapping("SBI", "TSE") is None
    # ...while what was tried survives for the next person to read.
    rows = await _mappings(db, "SBI")
    assert len(rows) == 1 and rows[0].is_active is False


@pytest.mark.asyncio
async def test_purge_prices_clears_only_that_securitys_prices(db):
    """The documented recovery in one step. Scope matters: purging a neighbour's cache
    would trigger a needless Yahoo refetch, which rule 1 exists to avoid."""
    await cli.run(args("set", "SBI", "TSE", "SBI"))

    assert await cli.run(args("disable", "SBI", "TSE", "--purge-prices")) == 0

    assert await _prices(db, 1) == []
    assert len(await _prices(db, 2)) == 1        # AMZN untouched


@pytest.mark.asyncio
async def test_disable_dry_run_changes_nothing(db):
    await cli.run(args("set", "SBI", "TSE", "SBI.TO"))

    assert await cli.run(args("disable", "SBI", "TSE", "--purge-prices", "--dry-run")) == 0

    assert await TickerMappingRepository(db).get_mapping("SBI", "TSE") is not None
    assert len(await _prices(db, 1)) == 2


@pytest.mark.asyncio
async def test_disabling_something_that_is_not_mapped_fails_cleanly(db):
    assert await cli.run(args("disable", "GHOST", "NOWHERE")) == 1

    runs = await _runs(db)
    assert [r.status for r in runs] == ["error"]


@pytest.mark.asyncio
async def test_purge_prices_without_a_security_refuses_rather_than_half_working(db):
    await cli.run(args("set", "2330", "TWSE", "2330.TW"))

    assert await cli.run(args("disable", "2330", "TWSE", "--purge-prices")) == 1

    # The mapping must still be active: the command was refused, not partly applied.
    assert await TickerMappingRepository(db).get_mapping("2330", "TWSE") is not None


# --- list -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_surfaces_a_currency_disagreement_already_in_the_table(db, capsys):
    """The whole point of the command: a bad row should be visible here rather than being
    discovered as a 61% overstatement in the portfolio total."""
    db.add(TickerMapping(ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI.L",
                         source="auto", is_active=True))
    await db.commit()

    assert await cli.run(args("list")) == 0

    out = capsys.readouterr().out
    assert "CURRENCY DISAGREEMENT" in out
    assert "security is CAD, ticker implies GBP" in out


@pytest.mark.asyncio
async def test_list_reports_a_clean_table_and_the_price_counts(db, capsys):
    await cli.run(args("set", "SBI", "TSE", "SBI.TO"))
    capsys.readouterr()

    assert await cli.run(args("list")) == 0

    out = capsys.readouterr().out
    assert "No currency disagreements." in out
    assert "SBI@TSE" in out and "SBI.TO" in out
    assert "2" in out          # SBI's two cached prices


@pytest.mark.asyncio
async def test_list_is_fine_with_an_empty_table(db, capsys):
    assert await cli.run(args("list")) == 0
    assert "No ticker mappings." in capsys.readouterr().out


# --- a mapping change retires the dividend rows it produced ---------------------------
#
# The SBI recovery purged prices and refetched them, but nothing invalidated the
# dividend estimates fetched under the wrong ticker. They survived, and because
# _forecast_inputs() lets whichever series carries amount_per_share define the
# cadence, those rows went on projecting a schedule the company does not have.


def _estimate(security_id, ex_date, computed):
    from app.models.dividend_payment import DividendPayment
    return DividendPayment(
        security_id=security_id, ex_date=ex_date, source="yfinance_estimate",
        amount_per_share=Decimal("0.042"), currency="CAD",
        shares_held=Decimal("50"), gross_amount_eur=Decimal("1.30"),
        last_computed=computed,
    )


def _ibkr_row(security_id, on_date):
    from app.models.dividend_payment import DividendPayment
    return DividendPayment(
        security_id=security_id, ex_date=on_date, pay_date=on_date, source="ibkr",
        amount_per_share=None, currency="CAD", shares_held=Decimal("0"),
        gross_amount_eur=Decimal("2.91"), withholding_tax_eur=Decimal("0"),
        net_amount_eur=Decimal("2.91"),
    )


async def _dividends(db, security_id):
    from app.models.dividend_payment import DividendPayment
    rows = (await db.execute(
        select(DividendPayment).where(DividendPayment.security_id == security_id)
    )).scalars().all()
    return sorted((r.ex_date, r.source) for r in rows)


@pytest.mark.asyncio
async def test_purge_clears_dividend_estimates_but_keeps_ibkr_rows(db):
    from datetime import datetime
    db.add_all([
        _estimate(1, date(2026, 6, 23), datetime(2026, 6, 24)),
        _estimate(1, date(2026, 7, 24), datetime(2026, 7, 25)),
        _ibkr_row(1, date(2026, 7, 10)),
    ])
    await TickerMappingRepository(db).upsert_mapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI", source="auto",
    )
    await db.commit()

    rc = await cli.run(args("disable", "SBI", "TSE", "--purge-prices"))

    assert rc == 0
    assert await _prices(db, 1) == []
    # The estimates go; the authoritative IBKR payment stays.
    assert await _dividends(db, 1) == [(date(2026, 7, 10), "ibkr")]

    run = (await _runs(db))[-1]
    assert run.details["dividend_estimates_deleted"] == 2
    assert run.details["prices_deleted"] == 2


@pytest.mark.asyncio
async def test_dry_run_purge_reports_dividends_and_deletes_nothing(db, capsys):
    from datetime import datetime
    db.add_all([_estimate(1, date(2026, 6, 23), datetime(2026, 6, 24))])
    await TickerMappingRepository(db).upsert_mapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI", source="auto",
    )
    await db.commit()

    assert await cli.run(args("disable", "SBI", "TSE", "--purge-prices", "--dry-run")) == 0

    out = capsys.readouterr().out
    assert "1 dividend estimate(s)" in out
    assert len(await _prices(db, 1)) == 2
    assert len(await _dividends(db, 1)) == 1


@pytest.mark.asyncio
async def test_changing_a_ticker_warns_that_its_estimates_are_now_suspect(db, capsys):
    from datetime import datetime
    db.add_all([
        _estimate(1, date(2026, 6, 23), datetime(2026, 6, 24)),
        _estimate(1, date(2026, 7, 24), datetime(2026, 7, 25)),
    ])
    await TickerMappingRepository(db).upsert_mapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI", source="auto",
    )
    await db.commit()

    # Correcting the bare ticker to the Toronto listing is exactly when those
    # rows stopped being trustworthy.
    assert await cli.run(args("set", "SBI", "TSE", "SBI.TO")) == 0

    out = capsys.readouterr().out
    assert "2 yfinance dividend estimate(s)" in out
    assert "are now suspect" in out
    assert "purge_dividend_estimates SBI TSE" in out


@pytest.mark.asyncio
async def test_setting_an_unchanged_ticker_does_not_cry_wolf(db, capsys):
    from datetime import datetime
    db.add_all([_estimate(1, date(2026, 6, 23), datetime(2026, 6, 24))])
    await TickerMappingRepository(db).upsert_mapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI.TO", source="manual",
    )
    await db.commit()

    assert await cli.run(args("set", "SBI", "TSE", "SBI.TO", "--notes", "re-pin")) == 0
    assert "suspect" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_list_flags_dividends_that_predate_their_mapping(db, capsys):
    from datetime import datetime
    db.add_all([_estimate(1, date(2026, 6, 23), datetime(2026, 6, 24))])
    await TickerMappingRepository(db).upsert_mapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI.TO", source="manual",
    )
    await db.commit()
    # The mapping was corrected after those rows were fetched.
    mapping = (await _mappings(db, "SBI"))[0]
    mapping.updated_at = datetime(2026, 7, 27)
    await db.commit()

    assert await cli.run(args("list")) == 0

    out = capsys.readouterr().out
    assert "DIVIDENDS PREDATE MAPPING" in out
    assert "purge_dividend_estimates SBI TSE" in out


# ── Verifying a mapping against NAVs the provider published ─────────────────

class _FakeHistory:
    """Minimal stand-in for a yfinance history frame: `.empty` and `.iterrows()`."""

    def __init__(self, rows):
        self._rows = rows

    @property
    def empty(self):
        return not self._rows

    def iterrows(self):
        for when, close in self._rows:
            yield SimpleNamespace(date=lambda w=when: w), {"Close": close}


def _stub_yahoo(monkeypatch, rows):
    """No network. `rows` is [(date, close)] — what the candidate ticker would return."""
    class _Ticker:
        def __init__(self, _t):
            pass

        def history(self, **_kw):
            return _FakeHistory(rows)

    monkeypatch.setattr(cli.yf, "Ticker", _Ticker)


async def _fund(session, *, navs):
    """A statement-priced fund, the shape the finpension importer creates."""
    session.add(Security(
        id=9, isin="CH1529078078", symbol="CH1529078078", description="Swisscanto EM",
        currency="CHF", conid=None, asset_category="STK", exchange="FUND",
        account="pillar3a", price_source="manual",
    ))
    for when, price in navs:
        session.add(MarketPrice(
            security_id=9, date=when, close_price=Decimal(str(price)),
            currency="CHF", source="finpension_statement",
        ))
    await session.commit()


@pytest.mark.asyncio
async def test_a_different_share_class_is_refused_however_well_the_name_matches(
    db, monkeypatch
):
    """
    The measured case. `0P0000S0OE.SW` is "Swisscanto (CH) Index Equity Fund Emerging
    Markets" — a perfect name match for this holding, and the fund's *NT* tranche rather
    than the *NMT* one it actually holds. It quotes ~49% higher. Adopting it would carry
    the position half again too high and nothing downstream could see it: the SBI
    failure, at the same order of magnitude.

    A provider statement is the only oracle strong enough to catch this, which is the
    whole reason the check exists here and not for an ordinary listing.
    """
    await _fund(db, navs=[(date(2026, 9, 1), "121.201101")])
    _stub_yahoo(monkeypatch, [(date(2026, 9, 1), 180.83)])

    with pytest.raises(cli.MappingError) as exc:
        await cli.cmd_set(db, "CH1529078078", "FUND", "0P0000S0OE.SW",
                          notes=None, dry_run=False)
    message = str(exc.value)
    assert "different share class" in message
    assert "49" in message or "%" in message

    security = await db.get(Security, 9)
    assert security.price_source == "manual"   # unchanged
    assert await TickerMappingRepository(db).get_mapping("CH1529078078", "FUND") is None


@pytest.mark.asyncio
async def test_a_verified_ticker_is_saved_and_flips_the_security_onto_yahoo(
    db, monkeypatch
):
    """
    Saving the row is only half the action: `sync_securities` skips a manual security,
    so a mapping alone would be inert. The flip happens here and deliberately nowhere
    else — a ticker typed without the NAV check does not earn it.
    """
    await _fund(db, navs=[(date(2026, 9, 1), "448.528100")])
    # The real measured shape: Yahoo strikes on a different clock, so the surrounding
    # days straddle the statement figure by a few tenths.
    _stub_yahoo(monkeypatch, [
        (date(2026, 8, 31), 447.699188),
        (date(2026, 9, 1), 446.870392),
        (date(2026, 9, 2), 448.321594),
    ])

    code, details = await cli.cmd_set(db, "CH1529078078", "FUND", "0P0000S0OD.SW",
                                      notes=None, dry_run=False)
    assert code == 0
    assert details["price_source_flipped_to_yahoo"] is True

    security = await db.get(Security, 9)
    assert security.price_source == "yahoo"
    mapping = await TickerMappingRepository(db).get_mapping("CH1529078078", "FUND")
    assert mapping.yahoo_ticker == "0P0000S0OD.SW"


@pytest.mark.asyncio
async def test_a_ticker_with_no_prices_at_all_is_refused(db, monkeypatch):
    await _fund(db, navs=[(date(2026, 9, 1), "121.201101")])
    _stub_yahoo(monkeypatch, [])

    with pytest.raises(cli.MappingError) as exc:
        await cli.cmd_set(db, "CH1529078078", "FUND", "NOTATICKER",
                          notes=None, dry_run=False)
    assert "no prices at all" in str(exc.value)


@pytest.mark.asyncio
async def test_a_security_with_no_published_navs_is_unaffected(db, monkeypatch):
    """
    The check is a no-op for an ordinary listing, which is every security but these.
    It must not become a Yahoo call on every `set`, nor block one.
    """
    called = []
    monkeypatch.setattr(cli.yf, "Ticker",
                        lambda t: called.append(t) or SimpleNamespace())

    code, _ = await cli.cmd_set(db, "AMZN", "NASDAQ", "AMZN",
                                notes=None, dry_run=False)
    assert code == 0
    assert called == []


@pytest.mark.asyncio
async def test_a_dry_run_verifies_but_writes_nothing(db, monkeypatch):
    """
    The verification is the useful half of a dry run here — it is how you find out
    whether a candidate is the right instrument before committing to it.
    """
    await _fund(db, navs=[(date(2026, 9, 1), "448.528100")])
    _stub_yahoo(monkeypatch, [(date(2026, 9, 1), 448.32)])

    code, details = await cli.cmd_set(db, "CH1529078078", "FUND", "0P0000S0OD.SW",
                                      notes=None, dry_run=True)
    assert code == 0
    assert details == {}
    security = await db.get(Security, 9)
    assert security.price_source == "manual"
    assert await TickerMappingRepository(db).get_mapping("CH1529078078", "FUND") is None
