"""
An SEC filer quoted in another currency than it reports in (docs/company-financials.md,
*Prices*): TSMC's ADR closes in USD match none of its TWD figures, so its page had no
multiple. A held listing the identity store folds with it (2330@TWSE, by the issuer
override) is its ordinary share when the ADR stands at a whole number of it and the EPS is
per reported share; its closes then price the figures. Offline.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models.company_financials import CompanyFinancial, CompanyPrice, SecCompany
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.services import company_financials_service
from app.services.company_financials_service import CompanyFinancialsService
from tests.test_company_esef import _hold

TSM = 1046179
ADR_ISIN = "US8740391003"
ORD_ISIN = "TW0002330008"
FRIDAY = date(2026, 11, 13)
USD_EUR, TWD_EUR = 0.86, 0.0268
EPS = 45.25


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(company_financials_service, "utc_today", lambda: date(2026, 11, 16))


async def _tsmc(db, adr_close: float, eps_shares: float = 25.93e9):
    """TSMC's FY2025 in TWD, its ADR's USD close, 2330's TWD close, both rates."""
    db.add(SecCompany(cik=TSM, ticker="TSM", name="Taiwan Semiconductor", isin=ADR_ISIN,
                      fetched_at=datetime(2026, 11, 1), universe_seen_at=datetime(2026, 11, 1)))
    for metric, value, unit in (
        ("revenue", 3.81e12, "TWD"),
        ("net_income", EPS * 25.93e9, "TWD"),
        ("eps_diluted", EPS, "TWD/shares"),
        ("diluted_shares", eps_shares, "shares"),
    ):
        db.add(CompanyFinancial(
            cik=TSM, period_end=date(2025, 12, 31), fiscal_year=2025, fiscal_period="FY",
            metric=metric, value=value, unit=unit, source_concept=f"ifrs-full:{metric}",
            filed=date(2026, 4, 16), fetched_at=datetime(2026, 11, 1),
        ))
    db.add(CompanyPrice(cik=TSM, date=FRIDAY, close=adr_close, currency="USD"))
    await _hold(db, 4, "2330", ORD_ISIN, None)
    db.add(MarketPrice(security_id=4, date=FRIDAY, close_price=Decimal("1460"),
                       currency="TWD", source="yahoo"))
    for cur, rate in (("USD", USD_EUR), ("TWD", TWD_EUR)):
        db.add(ExchangeRate(date=FRIDAY, from_currency=cur, to_currency="EUR",
                            rate=Decimal(str(rate)), source="frankfurter"))
    await db.commit()


async def _key_stats(db):
    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(await svc.company_by_key(f"sec:{TSM}"), "annual", "max")
    return page["key_stats"]


@pytest.mark.asyncio
async def test_an_adr_filer_is_priced_by_its_ordinary_shares_listing(db, today):
    # One ADR is five shares: 5 x 1460 TWD at 0.0268/0.86 TWD per USD is 227.5 USD; at a
    # 3% premium, 234.3.
    await _tsmc(db, adr_close=234.3)
    stats = await _key_stats(db)
    assert stats["pe_ratio"] == pytest.approx(1460 / EPS)


@pytest.mark.asyncio
async def test_a_new_york_registry_share_is_its_ordinary_share_converted(db, today):
    # ASML's New York share has a CINS ISIN: its USD closes, converted, price EUR figures.
    db.add(SecCompany(cik=937966, ticker="ASML", name="ASML Holding NV", isin="USN070592100",
                      fetched_at=datetime(2026, 11, 1), universe_seen_at=datetime(2026, 11, 1)))
    for metric, value, unit in (
        ("revenue", 32.67e9, "EUR"),
        ("net_income", 9.6094e9, "EUR"),
        ("eps_diluted", 24.71, "EUR/shares"),
        ("diluted_shares", 388.9e6, "shares"),
    ):
        db.add(CompanyFinancial(
            cik=937966, period_end=date(2025, 12, 31), fiscal_year=2025, fiscal_period="FY",
            metric=metric, value=value, unit=unit, source_concept=metric,
            filed=date(2026, 2, 11), fetched_at=datetime(2026, 11, 1),
        ))
    db.add(CompanyPrice(cik=937966, date=FRIDAY, close=1495.16, currency="USD"))
    db.add(ExchangeRate(date=FRIDAY, from_currency="USD", to_currency="EUR",
                        rate=Decimal(str(USD_EUR)), source="frankfurter"))
    await db.commit()
    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(await svc.company_by_key("sec:937966"), "annual", "max")
    assert page["key_stats"]["pe_ratio"] == pytest.approx(1495.16 * USD_EUR / 24.71)


@pytest.mark.asyncio
async def test_no_whole_ratio_or_an_eps_per_receipt_prices_nothing(db, today):
    # 0.8 of a share: a preferred's discount, not a receipt of whole shares.
    await _tsmc(db, adr_close=1460 * TWD_EUR / USD_EUR * 0.8)
    assert (await _key_stats(db))["pe_ratio"] is None


@pytest.mark.asyncio
async def test_an_eps_not_per_reported_share_prices_nothing(db, today):
    # Net income over EPS is a fifth of the stated count: the EPS is per ADR.
    await _tsmc(db, adr_close=234.3, eps_shares=25.93e9 * 5)
    assert (await _key_stats(db))["pe_ratio"] is None
