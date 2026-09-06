"""
Pillar 3a is outside the Swiss tax base, and the report must say so.

The highest-stakes file in this feature. Swiss 3a capital is **not** part of the
Steuerwert — it is taxed on withdrawal, at a separate reduced rate — and 3a income is
**not** taxable income, so there is no DA-1 reclaim to file. Including any of it
produces a wrong tax return, and the DA-1 case is the sharpest: both Swisscanto ISINs
begin `CH`, and the country bucket is keyed on `isin[:2]`, so 3a income would land in
the one bucket that is definitionally not a foreign-withholding reclaim.

Its *contributions* are a real tax figure and are reported separately, being
deductible from taxable income.

EUR base so no FX arithmetic is in the way. Offline: no network.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.accounts import IBKR, PILLAR3A, is_tax_exempt, tax_exempt_accounts
from app.database import Base
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN
from app.models.dividend_payment import DividendPayment
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.services.tax_service import TaxService


# A *past* year on purpose: the Steuerwert is the 31 December value, so this
# exercises the reconstruct-at-year-end path rather than today's snapshot.
YEAR = 2025
ON = date(YEAR, 12, 31)


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, AsyncSession(engine, expire_on_commit=False)


async def _seed(session):
    """One IBKR holding and one 3a holding, each with income and a sale."""
    session.add_all([
        Security(id=1, isin="US1111111111", symbol="AAA", description="US Co",
                 currency="EUR", conid=1, exchange="NASDAQ", account=IBKR),
        Security(id=2, isin="CH0117044948", symbol="CH0117044948",
                 description="Swisscanto World ex CH", currency="EUR", conid=None,
                 exchange="FUND", account=PILLAR3A),
    ])
    await session.flush()

    session.add_all([
        # Held at year end, both accounts, both priced.
        TaxLot(security_id=1, open_date=date(YEAR, 1, 5), quantity=Decimal("10"),
               cost_basis=Decimal("1000"), price_per_unit=Decimal("100"),
               currency="EUR", cost_basis_eur=Decimal("1000"), is_open=True),
        TaxLot(security_id=2, open_date=date(YEAR, 1, 5), quantity=Decimal("4"),
               cost_basis=Decimal("2000"), price_per_unit=Decimal("500"),
               currency="EUR", cost_basis_eur=Decimal("2000"), is_open=True),
        MarketPrice(security_id=1, date=ON, close_price=Decimal("110"),
                    currency="EUR", source="test"),
        MarketPrice(security_id=2, date=ON, close_price=Decimal("550"),
                    currency="EUR", source="test"),
        # Income on both. A 3a dividend row should not exist in practice — the
        # importer books distributions to cash_flows — but the guard must hold if one
        # ever does, which is the whole reason it is written.
        DividendPayment(security_id=1, ex_date=date(YEAR, 2, 1), pay_date=date(YEAR, 2, 1),
                        gross_amount_eur=Decimal("50"), withholding_tax_eur=Decimal("7.5"),
                        net_amount_eur=Decimal("42.5"), source="ibkr", currency="EUR"),
        DividendPayment(security_id=2, ex_date=date(YEAR, 2, 2), pay_date=date(YEAR, 2, 2),
                        gross_amount_eur=Decimal("999"), withholding_tax_eur=Decimal("0"),
                        net_amount_eur=Decimal("999"), source="ibkr", currency="EUR"),
        # A sale in each account.
        Trade(ib_key="ibkr-sell", conid="1", security_id=1, symbol="AAA",
              trade_date=date(YEAR, 2, 10), buy_sell="SELL", quantity=Decimal("-1"),
              price=Decimal("120"), proceeds=Decimal("120"), commission=Decimal("0"),
              realized_pnl=Decimal("20"), currency="EUR", account=IBKR),
        Trade(ib_key="fp-sell", conid="CH0117044948", security_id=2,
              symbol="CH0117044948", trade_date=date(YEAR, 2, 11), buy_sell="SELL",
              quantity=Decimal("-1"), price=Decimal("540"), proceeds=Decimal("540"),
              commission=Decimal("0"), realized_pnl=Decimal("40"), currency="EUR",
              account=PILLAR3A),
        # Contributions: only the deposits are deductible.
        CashFlow(ib_key="fp-d1", flow_date=date(YEAR, 1, 3), flow_type=DEPOSIT_WITHDRAW,
                 amount=Decimal("1758"), currency="EUR", amount_eur=Decimal("1758"),
                 account=PILLAR3A),
        CashFlow(ib_key="fp-t1", flow_date=date(YEAR, 1, 4), flow_type=TRANSFER_IN,
                 amount=Decimal("50000"), currency="EUR", amount_eur=Decimal("50000"),
                 account=PILLAR3A),
        CashFlow(ib_key="ibkr-d1", flow_date=date(YEAR, 1, 2), flow_type=DEPOSIT_WITHDRAW,
                 amount=Decimal("500"), currency="EUR", amount_eur=Decimal("500"),
                 account=IBKR),
    ])
    await session.flush()


@pytest.mark.asyncio
async def test_pillar3a_is_absent_from_all_three_taxable_sections():
    engine, session = await _make_session()
    try:
        await _seed(session)
        report = await TaxService(session).get_tax_report(YEAR)

        # 1. Dividend income — the 999 must not appear anywhere.
        assert report["dividend_totals"]["gross"] == 50.00
        assert all(row["symbol"] != "CH0117044948" for row in report["dividend_income"])

        # 2. DA-1 — and specifically no CH bucket, which is the sharpest failure:
        #    a Swiss ISIN in a foreign-withholding reclaim is not merely extra income,
        #    it is a claim against a country that withheld nothing.
        assert "CH" not in {row["country"] for row in report["dividend_by_country"]}

        # 3. Realized gains — filtered on Trade.account, not the joined security.
        assert report["realized_totals"]["gain_loss"] == 20.00
        assert all(row["symbol"] != "CH0117044948" for row in report["realized_gains"])

        # 4. Steuerwert — the wealth-tax base.
        assert [row["symbol"] for row in report["holdings_snapshot"]] == ["AAA"]
        assert report["holdings_snapshot_total"] == 10 * 110.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unlinked_ibkr_trade_survives_the_realized_filter():
    """
    `Trade.security_id` is nullable by design — a fully-sold security is absent from
    OpenPositions — and the join is an OUTER one for exactly that reason. Filtering on
    the joined `Security.account` instead of on `Trade.account` would silently drop
    every unlinked IBKR sale along with the 3a ones, which is a much bigger hole than
    the one being closed.
    """
    engine, session = await _make_session()
    try:
        await _seed(session)
        session.add(Trade(
            ib_key="ibkr-orphan", conid="999", security_id=None, symbol="GONE",
            trade_date=date(YEAR, 2, 12), buy_sell="SELL", quantity=Decimal("-5"),
            price=Decimal("10"), proceeds=Decimal("50"), commission=Decimal("0"),
            realized_pnl=Decimal("5"), currency="EUR", account=IBKR,
        ))
        await session.flush()

        report = await TaxService(session).get_tax_report(YEAR)
        assert "GONE" in {row["symbol"] for row in report["realized_gains"]}
        assert report["realized_totals"]["gain_loss"] == 25.00
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_deductible_figure_is_deposits_only():
    """
    A `TRANSFER_IN` is capital moved from another 3a or vested-benefits account. It was
    deducted in the year it was originally paid in, so counting it would overstate this
    year's deduction — the error direction that costs money at an audit. Also: the
    IBKR deposit is not a 3a contribution and must not leak in.
    """
    engine, session = await _make_session()
    try:
        await _seed(session)
        report = await TaxService(session).get_tax_report(YEAR)

        assert report["pillar3a"]["tracked"] is True
        assert report["pillar3a"]["contributions"] == 1758.00
        assert report["pillar3a"]["accounts"] == [PILLAR3A]
        # The excluded assets are stated rather than left unexplained.
        assert report["pillar3a"]["holdings_value"] == 4 * 550.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_excluded_security_is_never_reported_as_an_unvaluable_one():
    """
    `last_snapshot_skipped` means "could not be valued", and the report turns it into a
    warning that the wealth-tax base understates. A deliberate exclusion reported
    through that latch would make the one figure that goes on a tax return look broken
    — and would train the reader to ignore the warning that matters.
    """
    engine, session = await _make_session()
    try:
        await _seed(session)
        # Make the 3a fund genuinely unpriceable as well as excluded.
        await session.execute(MarketPrice.__table__.delete().where(
            MarketPrice.__table__.c.security_id == 2))
        await session.flush()

        report = await TaxService(session).get_tax_report(YEAR)
        assert not any("CH0117044948" in w for w in report["warnings"])
        assert not any("could not be valued" in w for w in report["warnings"])
        assert report["holdings_snapshot_total"] == 10 * 110.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_exclusion_says_why_rather_than_merely_that():
    """
    A Steuerwert that omits a holding without saying so is the failure this report was
    fixed for in August. Here the omission is deliberate, so the note must carry the
    *reason* — otherwise the next reader repairs it back.
    """
    engine, session = await _make_session()
    try:
        await _seed(session)
        report = await TaxService(session).get_tax_report(YEAR)

        note = " ".join(report["warnings"])
        assert "Pillar 3a" in note
        assert "withdrawal" in note        # the reason, not just the fact
        assert "deductible" in report["pillar3a"]["note"]

        csv = TaxService(session).to_csv(report)
        assert "Pillar 3a" in csv
        assert "1758" in csv
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_ibkr_only_database_reports_no_pillar3a_block():
    """
    Absent, not an empty block. `tracked: false` over zeros would assert the owner has
    a 3a account holding nothing, which is a different statement from having none.
    """
    engine, session = await _make_session()
    try:
        session.add(Security(id=1, isin="US1111111111", symbol="AAA",
                             description="US Co", currency="EUR", conid=1,
                             exchange="NASDAQ", account=IBKR))
        await session.flush()
        report = await TaxService(session).get_tax_report(YEAR)
        assert report["pillar3a"] is None
        assert not any("Pillar 3a" in w for w in report["warnings"])
    finally:
        await session.close()
        await engine.dispose()


# ── The predicate ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_second_3a_portfolio_inherits_the_treatment_without_an_edit():
    """
    Resolved from the data by prefix, so `--account pillar3a-2` is exempt the moment it
    exists. A set-membership test would silently tax the second portfolio.
    """
    assert is_tax_exempt("pillar3a")
    assert is_tax_exempt("pillar3a-2")
    assert not is_tax_exempt(IBKR)
    assert not is_tax_exempt(None)

    engine, session = await _make_session()
    try:
        session.add_all([
            Security(id=1, isin="US1111111111", symbol="A", description="A",
                     currency="EUR", conid=1, exchange="NASDAQ", account=IBKR),
            Security(id=2, isin="CH0117044948", symbol="B", description="B",
                     currency="EUR", conid=None, exchange="FUND", account="pillar3a-2"),
        ])
        await session.flush()
        assert await tax_exempt_accounts(session) == ["pillar3a-2"]
    finally:
        await session.close()
        await engine.dispose()
