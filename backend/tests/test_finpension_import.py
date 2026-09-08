"""
The finpension importer's database phase.

Offline: no network. The CLI's session factory is monkeypatched to an in-memory
database, and FX is a stub — the copy-at-each-date rule is exercised in
`test_contributions.py`, and stubbing it here keeps these tests about the ledger.

Every fixture is synthetic. The real export is account data and gitignored.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.accounts import IBKR, PILLAR3A
from app.cli import import_finpension_csv as cli
from app.database import Base
from app.models.cash_flow import (
    CashFlow, DEPOSIT_WITHDRAW, FEE as FLOW_FEE, INCOME as FLOW_INCOME,
    TRANSFER_IN as FLOW_TRANSFER_IN,
)
from app.models.market_price import MarketPrice
from app.models.security import PRICE_SOURCE_MANUAL, Security
from app.models.sync_run import SyncRun
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.repositories.cash_flow_repository import CashFlowRepository
from app.services.finpension_ingest import (
    CARRY_HORIZON_DAYS,
    PRICE_SOURCE_CARRY,
    PRICE_SOURCE_STATEMENT,
    FinpensionIngestError,
    ingest_finpension_report,
)
from app.services.finpension_report import EXPECTED_HEADER, parse_transaction_report


HEADER = ";".join(EXPECTED_HEADER)
EM = "CH1529078078"
WORLD = "CH0117044948"


def _row(d, category, cash, balance, *, name="", isin="", shares="", price=""):
    return (f'{d};{category};"{name}";{isin};{shares};CHF;1.0000000000;'
            f'{price};{cash};{balance}')


def _file(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


REAL_SHAPE = _file(
    _row("2026-08-25", "Deposit", "256.000000", "256.000000"),
    _row("2026-08-26", "Deposit", "1002.000000", "1258.000000"),
    _row("2026-08-28", "Deposit", "500.000000", "1758.000000"),
    _row("2026-09-01", "Buy", "-438.141980", "1319.858020",
         name="Swisscanto (CH) Index Equity Fund Emerging Markets NMT CHF",
         isin=EM, shares="3.615000", price="121.201101"),
    _row("2026-09-01", "Buy", "-1299.834434", "20.023586",
         name="Swisscanto (CH) IPF I Index Equity Fund World ex CH NT CHF",
         isin=WORLD, shares="2.898000", price="448.528100"),
)


class StubFx:
    """CHF -> EUR at a flat rate. The per-date rule is tested where it lives."""
    RATE = Decimal("1.05")

    def __init__(self, fails_on=()):
        self.fails_on = set(fails_on)

    async def convert_to_eur(self, amount, from_currency, target_date):
        if target_date in self.fails_on:
            raise ValueError("no rate")
        return amount * self.RATE


@pytest_asyncio.fixture
async def db(monkeypatch):
    """In-memory DB, wired in as the CLI's session factory so it needs no patching."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    class _Factory:
        def __call__(self):
            return _Ctx()

    monkeypatch.setattr(cli, "AsyncSessionLocal", _Factory())
    monkeypatch.setattr(cli, "CurrencyService", lambda _db: StubFx())
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def _apply(session, text, account=PILLAR3A, force=False, fx=None):
    return await ingest_finpension_report(
        session, parse_transaction_report(text), fx or StubFx(),
        account=account, force=force,
    )


async def _rows(session, model, **where):
    query = select(model)
    for column, value in where.items():
        query = query.where(getattr(model, column) == value)
    return list((await session.execute(query)).scalars().all())


# ── The happy path ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_real_shape_lands_as_two_holdings_and_three_deposits(db):
    result = await _apply(db, REAL_SHAPE)
    await db.commit()

    assert result["securities"] == 2
    assert result["trades"] == 2
    assert result["deposits"] == 3
    assert result["lots_opened"] == 2
    assert result["lots_closed"] == 0
    assert result["final_balance_chf"] == "20.023586"

    securities = await _rows(db, Security, account=PILLAR3A)
    assert {s.isin for s in securities} == {EM, WORLD}
    for security in securities:
        # No IBKR contract id is invented, and the fund is not handed to Yahoo until
        # a human validates a mapping against a statement NAV.
        assert security.conid is None
        assert security.price_source == PRICE_SOURCE_MANUAL
        assert security.currency == "CHF"
        # Non-null so `ticker_mappings` stays reachable — see FUND_EXCHANGE.
        assert security.exchange

    lots = await _rows(db, TaxLot)
    by_cost = {lot.cost_basis for lot in lots}
    # Cost basis is the cash flow, not shares x price (which is ...980115).
    assert Decimal("438.141980") in by_cost
    assert Decimal("1299.834434") in by_cost


@pytest.mark.asyncio
async def test_deposits_are_money_in_and_vested_benefits_are_not(db):
    """
    The rule 3a inherits from the IBKR side unchanged: a transfer moves capital saved
    earlier somewhere else, so counting it would invent savings in a month that had
    none. `get_deposits()`'s whitelist does the work with no new logic.
    """
    await _apply(db, _file(
        _row("2026-08-25", "Deposit", "1000.000000", "1000.000000"),
        _row("2026-08-26", "Transfer vested benefits", "50000.000000", "51000.000000"),
    ))
    await db.commit()

    flows = await _rows(db, CashFlow, account=PILLAR3A)
    assert {f.flow_type for f in flows} == {DEPOSIT_WITHDRAW, FLOW_TRANSFER_IN}

    counted = await CashFlowRepository(db).get_deposits()
    assert sum(f.amount for f in counted) == Decimal("1000.000000")


@pytest.mark.asyncio
async def test_fees_and_income_move_cash_and_never_reach_dividend_payments(db):
    """
    The exclusion achieved by not writing the row. A 3a distribution in
    `dividend_payments` would enter the era splice, the forecast and the DA-1 reclaim,
    each of which would then need a filter someone has to remember.
    """
    from app.models.dividend_payment import DividendPayment

    await _apply(db, _file(
        _row("2026-08-25", "Deposit", "1000.000000", "1000.000000"),
        _row("2026-09-30", "Flat-rate administrative fee", "-3.900000", "996.100000"),
        _row("2026-10-01", "Dividend", "12.000000", "1008.100000", isin=EM),
        _row("2026-10-02", "Interests", "0.150000", "1008.250000"),
    ))
    await db.commit()

    flows = {f.flow_type: f.amount for f in await _rows(db, CashFlow, account=PILLAR3A)}
    assert flows[FLOW_FEE] == Decimal("-3.900000")
    assert flows[FLOW_INCOME] == Decimal("0.150000")  # last INCOME row wins the dict
    assert await _rows(db, DividendPayment) == []

    # And the ledger still reconciles to finpension's own closing balance.
    total = sum(f.amount for f in await _rows(db, CashFlow, account=PILLAR3A))
    assert total == Decimal("1008.250000")


@pytest.mark.asyncio
async def test_a_sale_closes_lots_fifo_and_computes_realized_pnl(db):
    """
    `realized_pnl` must not be NULL: `_realized_from_trades` prefers the trades table
    wholesale once any SELL exists and reads `t.realized_pnl or 0`, so a NULL would
    report this sale's gain as exactly zero in the blended headline.
    """
    await _apply(db, _file(
        _row("2026-01-05", "Deposit", "1000.000000", "1000.000000"),
        _row("2026-01-06", "Buy", "-400.000000", "600.000000",
             isin=EM, shares="4.000000", price="100.000000"),
        _row("2026-02-06", "Buy", "-600.000000", "0.000000",
             isin=EM, shares="4.000000", price="150.000000"),
        # Sell 6 shares at 200: 4 from the 100-lot, 2 from the 150-lot.
        _row("2026-03-06", "Sell", "1200.000000", "1200.000000",
             isin=EM, shares="6.000000", price="200.000000"),
    ))
    await db.commit()

    sell = next(t for t in await _rows(db, Trade, account=PILLAR3A)
                if t.buy_sell == "SELL")
    # cost consumed = 400 (all of lot 1) + 300 (half of lot 2) = 700; proceeds 1200.
    assert sell.realized_pnl == Decimal("500.000000")
    assert sell.quantity == Decimal("-6.000000")

    closed = [lot for lot in await _rows(db, TaxLot) if not lot.is_open]
    assert len(closed) == 2
    assert all(lot.close_date == date(2026, 3, 6) for lot in closed)
    assert all(lot.close_source == "finpension" for lot in closed)

    # The remainder stays open under its ORIGINAL open date, pro-rata.
    still_open = [lot for lot in await _rows(db, TaxLot) if lot.is_open]
    assert len(still_open) == 1
    assert still_open[0].open_date == date(2026, 2, 6)
    assert still_open[0].quantity == Decimal("2.000000")
    assert still_open[0].cost_basis == Decimal("300.000000")


# ── Prices ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_statement_navs_and_carried_rows_are_tagged_apart(db):
    """
    Two source tags, because the staleness detector has to be able to ask for the
    newest *observed* NAV — a carry that looked like an observation would hide the
    very staleness it exists to bridge.
    """
    await _apply(db, REAL_SHAPE)
    await db.commit()

    prices = await _rows(db, MarketPrice)
    observed = [p for p in prices if p.source == PRICE_SOURCE_STATEMENT]
    carried = [p for p in prices if p.source == PRICE_SOURCE_CARRY]

    assert {p.date for p in observed} == {date(2026, 9, 1)}
    assert len(observed) == 2  # one per fund
    assert carried, "the carry must exist or the funds read unpriced within a fortnight"
    assert all(p.currency == "CHF" for p in prices)

    # Bounded, never run to today: an old upload must eventually go loudly unpriced
    # rather than quietly valuing the fund at a months-old NAV.
    assert max(p.date for p in carried) <= date(2026, 9, 1) + timedelta(
        days=CARRY_HORIZON_DAYS)

    # Each fund carries its own NAV forward, not some other fund's.
    navs = {EM: Decimal("121.201101"), WORLD: Decimal("448.528100")}
    by_id = {s.id: s.isin for s in await _rows(db, Security, account=PILLAR3A)}
    for price in carried:
        assert price.close_price == navs[by_id[price.security_id]]

    # Weekends are not priced — a NAV is struck on business days.
    assert all(p.date.weekday() < 5 for p in carried)


@pytest.mark.asyncio
async def test_a_later_nav_stops_the_earlier_carry(db):
    """A carry runs to the next observation, not through it."""
    await _apply(db, _file(
        _row("2026-01-05", "Deposit", "1000.000000", "1000.000000"),
        _row("2026-01-06", "Buy", "-100.000000", "900.000000",
             isin=EM, shares="1.000000", price="100.000000"),
        _row("2026-02-06", "Buy", "-150.000000", "750.000000",
             isin=EM, shares="1.000000", price="150.000000"),
    ))
    await db.commit()

    prices = {p.date: (p.close_price, p.source) for p in await _rows(db, MarketPrice)}
    assert prices[date(2026, 1, 6)] == (Decimal("100.000000"), PRICE_SOURCE_STATEMENT)
    assert prices[date(2026, 2, 6)] == (Decimal("150.000000"), PRICE_SOURCE_STATEMENT)
    assert prices[date(2026, 1, 30)][0] == Decimal("100.000000")  # still the old NAV
    assert prices[date(2026, 2, 20)][0] == Decimal("150.000000")  # now the new one


# ── Replace, idempotency and the shrink guard ───────────────────────────────

@pytest.mark.asyncio
async def test_reimporting_the_same_file_changes_nothing(db):
    first = await _apply(db, REAL_SHAPE)
    await db.commit()
    keys_before = {t.ib_key for t in await _rows(db, Trade)} | {
        f.ib_key for f in await _rows(db, CashFlow)}

    second = await _apply(db, REAL_SHAPE)
    await db.commit()
    keys_after = {t.ib_key for t in await _rows(db, Trade)} | {
        f.ib_key for f in await _rows(db, CashFlow)}

    assert keys_before == keys_after
    assert second["rows_restated"] == 0
    for field in ("trades", "cash_flows", "lots_opened", "securities"):
        assert first[field] == second[field]
    assert len(await _rows(db, Security, account=PILLAR3A)) == 2


@pytest.mark.asyncio
async def test_an_extended_export_adds_without_duplicating(db):
    await _apply(db, REAL_SHAPE)
    await db.commit()

    extended = REAL_SHAPE.rstrip("\n") + "\n" + _row(
        "2026-09-25", "Deposit", "500.000000", "520.023586") + "\n"
    result = await _apply(db, extended)
    await db.commit()

    assert result["deposits"] == 4
    assert result["rows_restated"] == 0
    assert len(await _rows(db, CashFlow, account=PILLAR3A)) == 4


@pytest.mark.asyncio
async def test_a_truncated_export_is_refused_before_anything_is_written(db):
    await _apply(db, REAL_SHAPE)
    await db.commit()

    truncated = _file(_row("2026-08-25", "Deposit", "256.000000", "256.000000"))
    with pytest.raises(FinpensionIngestError) as exc:
        await _apply(db, truncated)
    assert "full history" in str(exc.value)

    await db.rollback()
    assert len(await _rows(db, CashFlow, account=PILLAR3A)) == 3
    assert len(await _rows(db, Trade, account=PILLAR3A)) == 2


@pytest.mark.asyncio
async def test_force_applies_a_shorter_export(db):
    await _apply(db, REAL_SHAPE)
    await db.commit()

    truncated = _file(_row("2026-08-25", "Deposit", "256.000000", "256.000000"))
    await _apply(db, truncated, force=True)
    await db.commit()
    assert len(await _rows(db, CashFlow, account=PILLAR3A)) == 1


@pytest.mark.asyncio
async def test_a_restatement_is_counted_rather_than_silent(db):
    """
    The content hash covers the running Balance, so a backdated correction changes
    every downstream key. That makes "history was restated" countable instead of
    something you notice months later.
    """
    await _apply(db, REAL_SHAPE)
    await db.commit()

    corrected = _file(
        _row("2026-08-25", "Deposit", "256.000000", "256.000000"),
        _row("2026-08-26", "Deposit", "1000.000000", "1256.000000"),  # was 1002
        _row("2026-08-28", "Deposit", "500.000000", "1756.000000"),
        _row("2026-09-01", "Buy", "-438.141980", "1317.858020",
             isin=EM, shares="3.615000", price="121.201101"),
        _row("2026-09-01", "Buy", "-1297.834434", "20.023586",
             isin=WORLD, shares="2.898000", price="447.838100"),
    )
    result = await _apply(db, corrected)
    await db.commit()

    assert result["rows_restated"] >= 1
    assert any("restated" in w for w in result["warnings"])


# ── Account isolation, from the other side ──────────────────────────────────

@pytest.mark.asyncio
async def test_the_importer_only_ever_replaces_its_own_account(db):
    """
    The mirror of `test_account_isolation`: the IBKR sync may not touch 3a rows, and
    the 3a import may not touch IBKR ones. Wholesale replace is exactly the shape that
    gets this wrong.
    """
    db.add(Security(id=900, isin="US0000000001", symbol="AAA", description="A",
                    currency="USD", conid=900, exchange="NASDAQ", account=IBKR))
    await db.flush()
    db.add_all([
        Trade(ib_key="ibkr-1", conid="900", security_id=900, trade_date=date(2026, 5, 1),
              buy_sell="BUY", quantity=Decimal("1"), account=IBKR),
        CashFlow(ib_key="ibkr-2", flow_date=date(2026, 5, 1),
                 flow_type=DEPOSIT_WITHDRAW, amount=Decimal("100"),
                 amount_eur=Decimal("95"), account=IBKR),
        TaxLot(security_id=900, open_date=date(2026, 5, 1), quantity=Decimal("1"),
               cost_basis=Decimal("10"), price_per_unit=Decimal("10"), currency="USD",
               cost_basis_eur=Decimal("9.5"), is_open=True),
    ])
    await db.commit()

    await _apply(db, REAL_SHAPE)
    await _apply(db, REAL_SHAPE)  # twice: the replace path, not just the first insert
    await db.commit()

    assert len(await _rows(db, Trade, account=IBKR)) == 1
    assert len(await _rows(db, CashFlow, account=IBKR)) == 1
    assert len(await _rows(db, TaxLot, security_id=900)) == 1


# ── FX ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unconvertible_date_skips_its_row_and_says_so(db):
    """
    The third site of the `_to_eur` rule. The tax copy and the dividend copy were both
    fixed to return None on FX failure; storing the unconverted CHF amount in an EUR
    column is the bug those fixes were for.
    """
    result = await _apply(db, REAL_SHAPE, fx=StubFx(fails_on={date(2026, 8, 26)}))
    await db.commit()

    assert result["deposits"] == 2
    assert any("No CHF/EUR rate" in w for w in result["warnings"])
    assert all(f.amount_eur is not None
               for f in await _rows(db, CashFlow, account=PILLAR3A))


# ── The CLI contract ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing_and_records_no_sync_run(db, tmp_path):
    path = tmp_path / "report.csv"
    path.write_text(REAL_SHAPE, encoding="utf-8")

    assert await cli.ingest(path, PILLAR3A, dry_run=True, force=False) == 0
    assert await _rows(db, CashFlow) == []
    assert await _rows(db, SyncRun) == []


@pytest.mark.asyncio
async def test_the_cli_records_a_sync_run_on_success(db, tmp_path, monkeypatch):
    path = tmp_path / "report.csv"
    path.write_text(REAL_SHAPE, encoding="utf-8")

    assert await cli.ingest(path, PILLAR3A, dry_run=False, force=False) == 0

    runs = await _rows(db, SyncRun)
    assert len(runs) == 1
    assert runs[0].sync_type == "pillar3a_csv"
    assert runs[0].status == "success"
    assert runs[0].details["deposits"] == 3


@pytest.mark.asyncio
async def test_a_malformed_file_is_refused_without_a_sync_run(db, tmp_path):
    """A file we never read is not a database event."""
    path = tmp_path / "bad.csv"
    path.write_text(_file(
        _row("2026-08-25", "Rebalancing Levy", "-5.000000", "-5.000000")
    ), encoding="utf-8")

    assert await cli.ingest(path, PILLAR3A, dry_run=False, force=False) == 1
    assert await _rows(db, SyncRun) == []


async def _noop():
    return None


@pytest.mark.asyncio
async def test_a_reimport_does_not_unpin_a_verified_yahoo_mapping(db):
    """
    `price_source` is set on **creation only**, and this is the important half.

    Re-stating it on every upsert made each re-upload silently revert a fund a human
    had pinned to a verified Yahoo ticker back to statement pricing — and write back
    the carried rows that then shadow the feed. Nothing would have said so: the prices
    keep arriving, they are just weeks stale and flat.
    """
    await _apply(db, REAL_SHAPE)
    await db.commit()

    world = next(s for s in await _rows(db, Security, account=PILLAR3A)
                 if s.isin == WORLD)
    world.price_source = "yahoo"
    await db.commit()

    await _apply(db, REAL_SHAPE)
    await db.commit()

    world = next(s for s in await _rows(db, Security, account=PILLAR3A)
                 if s.isin == WORLD)
    assert world.price_source == "yahoo"
    # And no carry is written back for it, or the purge on the flip was pointless.
    carried = [p for p in await _rows(db, MarketPrice, security_id=world.id)
               if p.source == PRICE_SOURCE_CARRY]
    assert carried == []

    # The still-manual fund keeps its carry — that is what it is for.
    em = next(s for s in await _rows(db, Security, account=PILLAR3A) if s.isin == EM)
    assert em.price_source == PRICE_SOURCE_MANUAL
    assert [p for p in await _rows(db, MarketPrice, security_id=em.id)
            if p.source == PRICE_SOURCE_CARRY]
