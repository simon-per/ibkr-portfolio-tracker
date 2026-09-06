"""
Tests for PortfolioService.get_contributions — money in per month, spliced at the
deposit-coverage boundary: lot cost basis before it, real deposits from it onward.

The splice is what makes the metric survive a position rotation, so the rotation
case is pinned explicitly (test_a_rotation_does_not_inflate_money_in).

EUR base except where a test pins the CHF projection, and ``as_of`` is pinned in
every test so the trailing windows don't move with the calendar.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.app_settings import AppSetting
from app.models.exchange_rate import ExchangeRate
from app.models.cash_flow import (
    CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN,
)
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.portfolio_service import PortfolioService, _shift_months


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [Security.__table__, TaxLot.__table__, AppSetting.__table__,
              CashFlow.__table__, ExchangeRate.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="EUR", conid=100, asset_category="STK", exchange="XETRA",
    ))
    await session.flush()
    return engine, session


def _lot(open_date: date, cost: str, close_date: Optional[date] = None) -> TaxLot:
    return TaxLot(
        security_id=1,
        open_date=open_date,
        quantity=Decimal("10"),
        cost_basis=Decimal(cost),
        price_per_unit=Decimal(cost) / 10,
        currency="EUR",
        cost_basis_eur=Decimal(cost),
        is_open=close_date is None,
        close_date=close_date,
        close_source="trade" if close_date else None,
    )


def _flow(flow_date: date, amount: str, key: str,
          flow_type: str = DEPOSIT_WITHDRAW) -> CashFlow:
    return CashFlow(
        ib_key=key,
        flow_date=flow_date,
        flow_type=flow_type,
        amount=Decimal(amount),
        currency="EUR",
        amount_eur=Decimal(amount),
    )


def _window(report: dict, label: str) -> dict:
    return next(w for w in report["windows"] if w["label"] == label)


def _month(report: dict, month: str) -> dict:
    return next(m for m in report["monthly"] if m["month"] == month)


@pytest.mark.asyncio
async def test_buys_bucket_into_their_open_month():
    engine, session = await _make_session()
    try:
        session.add_all([
            _lot(date(2026, 1, 15), "1000"),
            _lot(date(2026, 1, 28), "500"),
            _lot(date(2026, 3, 4), "700"),
        ])
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        assert [m["month"] for m in report["monthly"]] == ["2026-01", "2026-03"]
        assert _month(report, "2026-01")["net_eur"] == 1500.0
        assert _month(report, "2026-03")["net_eur"] == 700.0
        assert report["first_contribution_date"] == "2026-01-15"
        assert report["base_currency"] == "EUR"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sale_releases_capital_in_its_close_month():
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2026, 1, 10), "1000", close_date=date(2026, 3, 20)))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        # Deployed in January, released in March: net zero overall. Asserted as whole
        # dicts on purpose — a key added to the series has to be looked at, not
        # absorbed. With no deposit ledger the method is "deployed", so money in IS
        # deployment and the two agree by definition.
        assert _month(report, "2026-01") == {
            "month": "2026-01", "money_in_eur": 1000.0,
            "deployed_eur": 1000.0, "net_eur": 1000.0,
        }
        assert _month(report, "2026-03") == {
            "month": "2026-03", "money_in_eur": 0.0,
            "deployed_eur": 0.0, "net_eur": -1000.0,
        }
        assert _window(report, "all")["net_eur"] == 0.0

        # The average is deployment, so the sale does not erase the January buy:
        # 1,000 was put to work regardless of it later coming back out.
        # 2026-01-10..2026-03-31 is 80 days = 2.63 months, so 1,000 / 2.63.
        w_all = _window(report, "all")
        assert w_all["deployed_eur"] == 1000.0
        assert w_all["months"] == pytest.approx(2.63, abs=0.01)
        assert w_all["avg_deployed_per_month_eur"] == pytest.approx(380.5, abs=0.5)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_partially_sold_lot_conserves_the_original_cost_in_the_open_month():
    """
    A partial sale splits the lot pro-rata (sync_helper Phase D), leaving an open
    remainder and a closed piece that share the original open_date. Both legs must
    land in the open month and sum back to the original cost.
    """
    engine, session = await _make_session()
    try:
        session.add_all([
            _lot(date(2026, 2, 5), "600"),                              # remainder, still open
            _lot(date(2026, 2, 5), "400", close_date=date(2026, 5, 9)),  # sold piece
        ])
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 5, 31))

        assert _month(report, "2026-02")["deployed_eur"] == 1000.0
        assert _month(report, "2026-05")["net_eur"] == -400.0
        assert _window(report, "all")["net_eur"] == 600.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_window_divisor_is_clamped_to_available_history():
    engine, session = await _make_session()
    try:
        # Four months of history, one 1,200 purchase at the start.
        session.add(_lot(date(2026, 1, 31), "1200"))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 5, 31))

        w12 = _window(report, "12m")
        assert w12["partial"] is True
        assert w12["months"] == pytest.approx(3.94, abs=0.02)   # ~120 days, not 12 months
        assert w12["avg_deployed_per_month_eur"] == pytest.approx(304.5, abs=1.0)

        # A window shorter than the history is not partial, and excludes the buy.
        w3 = _window(report, "3m")
        assert w3["partial"] is False
        assert w3["months"] == 3.0
        assert w3["net_eur"] == 0.0

        assert _window(report, "all")["partial"] is False
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_averages_per_window_and_the_cost_basis_identity():
    engine, session = await _make_session()
    try:
        # 1,000/month for 12 months, plus one 500 sale released in the final month.
        for i in range(12):
            session.add(_lot(_shift_months(date(2026, 6, 15), 11 - i), "1000"))
        session.add(_lot(date(2025, 8, 15), "500", close_date=date(2026, 6, 2)))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        # 12 x 1,000 deployed + the 500 lot = 12,500 deployed; 500 came back out.
        w_all = _window(report, "all")
        assert w_all["deployed_eur"] == 12500.0
        assert w_all["net_eur"] == 12000.0

        # 3M covers the Apr/May/Jun buys. The June sale reduces net but must NOT
        # reduce the average — the money was still deployed when it was deployed.
        w3 = _window(report, "3m")
        assert w3["months"] == 3.0
        assert w3["deployed_eur"] == 3000.0
        assert w3["net_eur"] == 2500.0
        assert w3["avg_deployed_per_month_eur"] == pytest.approx(1000.0, abs=0.01)

        w6 = _window(report, "6m")
        assert w6["deployed_eur"] == 6000.0
        assert w6["avg_deployed_per_month_eur"] == pytest.approx(1000.0, abs=0.01)

        # The 12M window reaches back past the first lot, so its divisor is clamped
        # to the 11.5 months that actually exist — 12,500 / 11.5, not / 12.
        w12 = _window(report, "12m")
        assert w12["partial"] is True
        assert w12["months"] == pytest.approx(11.50, abs=0.02)
        assert w12["deployed_eur"] == 12500.0
        assert w12["avg_deployed_per_month_eur"] == pytest.approx(1087.0, abs=1.0)

        # Identity: every lot is either still open or was released, so the monthly
        # net must sum to the cost basis of the open lots — 12,000.
        assert round(sum(m["net_eur"] for m in report["monthly"]), 2) == 12000.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_portfolio_reports_nothing_rather_than_dividing_by_zero():
    engine, session = await _make_session()
    try:
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        assert report == {
            "windows": [],
            "monthly": [],
            "first_contribution_date": None,
            "deposits_from": None,
            "coverage_from": None,
            "transfer_in_date": None,
            "base_currency": "EUR",
        }
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_portfolio_opened_today_does_not_divide_by_zero():
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2026, 6, 30), "800"))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        w_all = _window(report, "all")
        assert w_all["months"] > 0
        assert w_all["net_eur"] == 800.0
        assert w_all["avg_deployed_per_month_eur"] > 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_without_a_deposit_ledger_money_in_falls_back_to_deployment():
    """
    The state on first deploy, before the Flex Query delivers deposits. money_in must
    equal deployment rather than reporting zero, and the method must say so.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2026, 1, 15), "1000"))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        assert report["deposits_from"] is None
        assert report["coverage_from"] is None
        assert report["transfer_in_date"] is None
        w_all = _window(report, "all")
        assert w_all["money_in_method"] == "deployed"
        assert w_all["money_in_eur"] == 1000.0
        assert w_all["deployed_eur"] == 1000.0
        assert w_all["deposits_eur"] == 0.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_money_in_splices_lots_before_coverage_and_deposits_after():
    """
    The real shape of this account: investing history predates IBKR, so the deposit
    ledger only covers the tail. A window inside coverage uses deposits alone; one
    that spans the boundary uses lots up to it and deposits from it, divided by the
    window's FULL months — the two ranges together leave no hole.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2025, 1, 10), "5000"))          # pre-coverage investing
        for i, m in enumerate((2, 3, 4, 5, 6)):               # 1,000/mo from Feb 2026
            session.add(_flow(date(2026, m, 5), "1000", f"D{i}"))
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        # Stored coverage says 1 Jan, but the ledger's first row is 5 Feb, so that is
        # where the splice actually falls. No money moves — the lot is older than both.
        assert report["coverage_from"] == "2026-02-05"
        assert report["deposits_from"] == "2026-02-05"

        # 3M starts after the boundary: deposits only, no lot contribution.
        w3 = _window(report, "3m")
        assert w3["money_in_method"] == "deposits"
        assert w3["money_in_eur"] == 3000.0             # Apr/May/Jun
        assert w3["avg_money_in_per_month_eur"] == pytest.approx(1000.0, abs=0.01)

        # All time spans the boundary: the 5,000 lot plus all 5,000 of deposits,
        # over the FULL 17.7 months of history rather than a clamped span.
        w_all = _window(report, "all")
        assert w_all["money_in_method"] == "spliced"
        assert w_all["money_in_eur"] == 10000.0
        assert w_all["deposits_eur"] == 5000.0
        # 536 days / 30.4375 = 17.61 months, so 10,000 / 17.61.
        assert w_all["months"] == pytest.approx(17.61, abs=0.02)
        assert w_all["avg_money_in_per_month_eur"] == pytest.approx(567.86, abs=0.5)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_boundary_month_counts_deposits_not_purchases():
    """
    A purchase and a deposit in the same boundary month must not both count. Lots are
    summed strictly BEFORE coverage_from and deposits strictly FROM it, so the money
    is counted once — through the deposit, which is the authoritative side there.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2025, 6, 10), "800"))            # pre-coverage
        session.add(_lot(date(2026, 1, 20), "950"))            # post-coverage purchase
        session.add(_flow(date(2026, 1, 15), "900", "D1"))     # what funded it
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        w_all = _window(report, "all")
        # 800 (pre-coverage lot) + 900 (deposit) — NOT 800 + 900 + 950.
        assert w_all["money_in_eur"] == 1700.0
        # Deployment still sees both purchases; that is the difference between them.
        assert w_all["deployed_eur"] == 1750.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_coverage_cannot_start_before_the_ledger_has_any_row():
    """
    The real shape of this account's first weeks, and the reason the splice is clamped.

    A Year-to-Date statement reports from 1 January, but the IBKR account was not funded
    until the 9th. In that gap the deposits table is empty because the money was still
    going to the previous broker — so taking the statement's coverage claim at face value
    drops those purchases from BOTH sides: past the lot cutoff, with no deposit standing
    in for them. The boundary therefore moves forward to the ledger's first row.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2024, 5, 28), "5000"))          # pre-IBKR investing
        session.add(_lot(date(2026, 1, 5), "156.61"))         # bought at the old broker
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))    # account funded
        session.add(_flow(date(2026, 1, 10), "400", "D2"))
        session.add(_flow(date(2026, 1, 19), "0", "TR1", flow_type=TRANSFER_IN))
        session.add(_lot(date(2026, 1, 23), "200"))           # bought at IBKR, from D1/D2
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        # Not 2026-01-01, which is what the statement period claims.
        assert report["coverage_from"] == "2026-01-09"

        w_all = _window(report, "all")
        assert w_all["money_in_method"] == "spliced"
        assert w_all["deposits_eur"] == 1400.0
        # 5,000 + 156.61 pre-ledger cost basis + 1,400 of deposits. The 23 Jan purchase
        # is post-boundary and counts through the deposit that funded it, not twice.
        assert w_all["money_in_eur"] == 6556.61
        # Deployment still sees every purchase, which is the difference between them.
        assert w_all["deployed_eur"] == 5356.61
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_transfer_row_alone_anchors_the_ledger_start():
    """
    The clamp keys on the ledger's first row of ANY type, not the first deposit.

    An account opened by an in-kind transfer can trade before any cash is ever deposited.
    Anchoring on the first deposit would leave that window on the lot side, where a
    rotation inflates it — exactly what the splice exists to prevent.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2025, 3, 10), "9000"))          # transferred-in history
        session.add(_flow(date(2026, 1, 19), "0", "TR1", flow_type=TRANSFER_IN))
        session.add(_lot(date(2026, 1, 25), "800"))           # funded by a sale, not cash
        session.add(_flow(date(2026, 2, 5), "1000", "D1"))    # first actual deposit
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        assert report["coverage_from"] == "2026-01-19"        # the transfer, not 5 Feb
        w_all = _window(report, "all")
        # 9,000 pre-ledger + the 1,000 deposit. The 25 Jan purchase sits after the
        # boundary and contributes nothing — no new money arrived to fund it.
        assert w_all["money_in_eur"] == 10000.0
        assert w_all["deployed_eur"] == 9800.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rotation_does_not_inflate_money_in():
    """
    The regression that protects the Ireland->US ETF switch.

    Selling one ETF to buy another closes lots and opens new ones for the same money,
    so deployment counts it twice. No deposit occurs, so money_in must not move at all.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2026, 2, 10), "5000"))            # bought post-coverage
        session.add(_flow(date(2026, 2, 5), "5000", "D1"))      # the money that funded it
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        before = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        # Now rotate: close the lot and open an equal-cost replacement, no new cash.
        lot = (await session.execute(select(TaxLot))).scalars().one()
        lot.is_open = False
        lot.close_date = date(2026, 6, 1)
        lot.close_source = "trade"
        session.add(_lot(date(2026, 6, 1), "5000"))
        await session.commit()

        after = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        for label in ("all", "12m", "6m", "3m"):
            b, a = _window(before, label), _window(after, label)
            assert a["money_in_eur"] == b["money_in_eur"], f"{label} money_in moved"
            assert a["avg_money_in_per_month_eur"] == b["avg_money_in_per_month_eur"]

        # Deployment, by contrast, now double-counts the same 5,000 — which is why it
        # cannot be the headline once rotation starts.
        assert _window(after, "all")["deployed_eur"] == 10000.0
        assert _window(before, "all")["deployed_eur"] == 5000.0
        # And the rotation nets out, so the identity still holds.
        assert _window(after, "all")["net_eur"] == 5000.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rotation_does_not_inflate_the_monthly_money_in():
    """
    The monthly twin of the test above, and the one the chart reads.

    The windows were rotation-proof from the start; `monthly[]` carried no money in at
    all until 2026-09-06, so the only series a chart of contributions could draw was
    the gross one. On this account that meant a ~31k August bar against a few hundred
    francs of new money — right as an answer to "what was put to work", and wrong by
    two orders of magnitude as an answer to "what did I pay in".
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2026, 2, 10), "5000"))
        session.add(_flow(date(2026, 2, 5), "5000", "D1"))
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        # June: sell the lot and buy an equal-cost replacement. No new cash.
        held = (await session.execute(select(TaxLot))).scalars().one()
        held.is_open = False
        held.close_date = date(2026, 6, 1)
        held.close_source = "trade"
        session.add(_lot(date(2026, 6, 1), "5000"))
        session.add(_flow(date(2026, 6, 20), "300", "D2"))   # a real, small contribution
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        june = _month(report, "2026-06")
        assert june["deployed_eur"] == 5000.0    # gross: the rotation, counted
        assert june["money_in_eur"] == 300.0     # the deposit, and only the deposit
        assert june["net_eur"] == 0.0            # 5,000 in, 5,000 out

        # February is the contrast: there the deployment WAS the contribution.
        feb = _month(report, "2026-02")
        assert feb["deployed_eur"] == 5000.0
        assert feb["money_in_eur"] == 5000.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_month_with_a_deposit_and_no_purchase_appears_in_the_series():
    """
    The series was keyed on months with LOT activity, so a month that only received
    money had no row — the contribution simply absent from a chart of contributions,
    with `sum(monthly)` quietly short of the window that includes it.

    Latent for as long as lots came first: the in-kind broker transfer carried its
    2024-25 open dates, so every deposit had lot activity around it. A retirement
    account is the opposite and the common shape — the pillar 3a deposits landed
    2026-08-25 against purchases on 09-01.
    """
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 3, 5), "1000", "D1"))    # March: money in, nothing bought
        session.add(_lot(date(2026, 4, 2), "1000"))           # April: invested
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 4, 30))

        assert [m["month"] for m in report["monthly"]] == ["2026-03", "2026-04"]
        march = _month(report, "2026-03")
        assert march["money_in_eur"] == 1000.0
        assert march["deployed_eur"] == 0.0      # nothing was bought, and that is honest
        april = _month(report, "2026-04")
        assert april["money_in_eur"] == 0.0      # the money arrived last month
        assert april["deployed_eur"] == 1000.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["rotation", "deposit_only_month", "no_ledger"])
async def test_the_monthly_series_and_the_windows_agree_about_money_in(shape):
    """
    The identity, in family form: `Σ monthly[].money_in_eur == windows['all']`.

    Both read `money_in_legs`, so they cannot disagree unless one of them re-derives
    the splice or drops a month — which are exactly the two ways this has already gone
    wrong once each. A fourth reader has to satisfy this too.
    """
    engine, session = await _make_session()
    try:
        if shape == "rotation":
            session.add(_lot(date(2026, 2, 10), "5000", close_date=date(2026, 6, 1)))
            session.add(_lot(date(2026, 6, 1), "5000"))
            session.add(_flow(date(2026, 2, 5), "5000", "D1"))
            await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        elif shape == "deposit_only_month":
            session.add(_flow(date(2026, 3, 5), "1000", "D1"))
            session.add(_flow(date(2026, 5, 5), "250", "D2"))
            session.add(_lot(date(2026, 4, 2), "1000"))
            await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        else:  # no deposit ledger at all: money in IS deployment
            session.add(_lot(date(2026, 1, 15), "800"))
            session.add(_lot(date(2026, 3, 4), "700", close_date=date(2026, 5, 9)))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 6, 30))

        assert round(sum(m["money_in_eur"] for m in report["monthly"]), 2) ==             _window(report, "all")["money_in_eur"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_withdrawal_reduces_money_in():
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2026, 1, 10), "1000"))
        session.add(_flow(date(2026, 2, 3), "2000", "D1"))
        session.add(_flow(date(2026, 3, 3), "-500", "W1"))
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        w_all = _window(report, "all")
        # The withdrawal is the thing under test: 2,000 in less 500 out.
        assert w_all["deposits_eur"] == 1500.0
        # Plus the January lot, which predates the ledger's first row (3 Feb) and so
        # is measured from cost basis rather than dropped.
        assert w_all["money_in_eur"] == 2500.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_transfer_is_never_counted_as_money_in():
    """
    An incoming broker transfer moves capital saved years earlier elsewhere, and the
    transferred lots already carry their own open_date — so counting its cash leg
    would both invent a contribution and double-count the original purchase.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2024, 6, 10), "20000"))         # transferred-in history
        session.add(_flow(date(2026, 1, 20), "18000", "TR1", flow_type=TRANSFER_IN))
        session.add(_flow(date(2026, 2, 5), "1000", "D1"))    # a real deposit
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 3, 31))

        # 20,000 pre-coverage lot + the genuine 1,000 deposit. The 18,000 transfer
        # contributes nothing, and neither does its cash leg.
        w_all = _window(report, "all")
        assert w_all["money_in_eur"] == 21000.0
        assert w_all["deposits_eur"] == 1000.0
        assert report["deposits_from"] == "2026-02-05"
        assert report["transfer_in_date"] == "2026-01-20"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_base_currency_projection_scales_both_metrics():
    """
    Production runs on CHF while the tests default to EUR, so this pins the path that
    projects both sides into the base currency: deployment at each lot's open_date,
    deposits at each flow's own date.
    """
    engine, session = await _make_session()
    try:
        settings = AppSettingsRepository(session)
        await settings.set_base_currency("CHF")
        # A flat EUR->CHF rate makes the expected values checkable by hand.
        for day in range(1, 32):
            session.add(ExchangeRate(
                date=date(2026, 1, day), from_currency="EUR", to_currency="CHF",
                rate=Decimal("0.9500"), source="test",
            ))
        session.add(_lot(date(2026, 1, 10), "1000"))
        session.add(_flow(date(2026, 1, 20), "400", "D1"))
        await settings.widen_cash_flows_covered_from(date(2026, 1, 1))
        await session.commit()

        report = await PortfolioService(session).get_contributions(as_of=date(2026, 1, 31))

        assert report["base_currency"] == "CHF"
        w_all = _window(report, "all")
        assert w_all["deployed_eur"] == 950.0    # 1000 EUR * 0.95
        # Spliced, so both conversion paths run: the lot at its open_date and the
        # deposit at its own flow_date. 950 + 380.
        assert w_all["money_in_method"] == "spliced"
        assert w_all["deposits_eur"] == 380.0    # 400 EUR * 0.95
        assert w_all["money_in_eur"] == 1330.0
    finally:
        await session.close()
        await engine.dispose()


def test_shift_months_clamps_to_the_shorter_target_month():
    assert _shift_months(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert _shift_months(date(2024, 5, 31), 3) == date(2024, 2, 29)   # leap year
    assert _shift_months(date(2026, 3, 15), 12) == date(2025, 3, 15)
    assert _shift_months(date(2026, 1, 10), 3) == date(2025, 10, 10)  # year boundary


@pytest.mark.asyncio
async def test_a_deposit_before_the_first_purchase_still_counts_as_money_in():
    """
    Every window is clamped to the start of history, and history used to start at the
    first *tax lot*. A deposit made before the first purchase therefore fell outside
    all of them and read as 0.00 money in against a full deployed figure.

    Invisible while there was one account: the IBKR holdings arrived by in-kind
    transfer carrying their original open dates, so the first lot predates the first
    deposit by years. A retirement account is the opposite shape and the common one —
    you pay in, and it is invested days later — which is how this surfaced. It was
    never 3a-specific: an IBKR deposit landing before the first purchase was always
    dropped the same way.
    """
    engine, session = await _make_session()
    try:
        settings = AppSettingsRepository(session)
        await settings.widen_cash_flows_covered_from(date(2026, 8, 25))
        session.add_all([
            _flow(date(2026, 8, 25), "256.00", "d1"),
            _flow(date(2026, 8, 26), "1002.00", "d2"),
            _flow(date(2026, 8, 28), "500.00", "d3"),
            # Invested a few days later, and for less than was paid in — the rest is
            # still sitting as cash, which is why deployed must be the smaller figure.
            _lot(date(2026, 9, 1), "1737.98"),
        ])
        await session.flush()

        report = await PortfolioService(session).get_contributions(
            as_of=date(2026, 9, 6)
        )
        all_time = _window(report, "all")

        assert all_time["money_in_eur"] == 1758.00
        assert all_time["deposits_eur"] == 1758.00
        assert all_time["money_in_method"] == "deposits"
        # The gap is the uninvested cash, not a lost deposit.
        assert all_time["deployed_eur"] == 1737.98
        # History is anchored on the first deposit, not the first buy.
        assert report["first_contribution_date"] == "2026-08-25"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_transferred_lot_still_anchors_history_before_the_ledger():
    """
    The other direction, so the widening above cannot become a replacement. A lot
    carried in from a previous broker predates every cash-flow row by design, and it
    must still set the start of history — otherwise the pre-IBKR years vanish from the
    all-time average and it divides by a few months instead of a few years.
    """
    engine, session = await _make_session()
    try:
        settings = AppSettingsRepository(session)
        await settings.widen_cash_flows_covered_from(date(2026, 1, 9))
        session.add_all([
            _lot(date(2024, 3, 15), "5000.00"),
            _flow(date(2026, 1, 9), "1000.00", "d1"),
        ])
        await session.flush()

        report = await PortfolioService(session).get_contributions(
            as_of=date(2026, 9, 6)
        )
        assert report["first_contribution_date"] == "2024-03-15"
        # Spliced: lot cost basis before the ledger, real deposits from it.
        assert _window(report, "all")["money_in_method"] == "spliced"
        assert _window(report, "all")["money_in_eur"] == 6000.00
    finally:
        await session.close()
        await engine.dispose()
