"""
Another regulator's company's closes without a Yahoo request (docs/company-financials.md,
*Prices without Yahoo*): the iShares rows' prices kept when the fund's declared currency holds
against the SEC companies' closes, a split read off the units, and the held listing's own closes
first. Offline.
"""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models.company_financials import CompanyPrice, SecCompany
from app.models.etf_basket import ConstituentPrice
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.services import company_financials_service, esef_sync
from app.services.company_financials_service import CompanyFinancialsService
from app.services.constituent_prices import basket_splits, record_constituent_prices
from app.services.esef_sync import EuropeFinancialsSync
from app.services.etf_basket_parsers import ConstituentRow, ParsedBasket, parse_ishares
from tests.test_company_esef import ISIN, LEI, Index, _hold, _none

IWDA = "IE00B4L5Y983"
AS_OF = date(2025, 12, 1)
USD_EUR = 0.86
# Five SEC filers in the basket, each with a weekly close in USD.
SEC = [(f"US00000000{i}{i}", 1000 + i, 100.0 + 10 * i) for i in range(5)]


@pytest.fixture
def dated(monkeypatch):
    monkeypatch.setattr(esef_sync, "utc_today", lambda: AS_OF)
    monkeypatch.setattr(esef_sync, "utcnow", lambda: datetime(2025, 12, 1, 5))
    monkeypatch.setattr(company_financials_service, "utc_today", lambda: AS_OF)


async def _schneider(db):
    """Schneider's two annual reports (FY2024 diluted EPS 7.53 EUR), its listing held."""
    await _hold(db, 1, "SU", ISIN, LEI)
    await EuropeFinancialsSync(db, transport=Index().transport, sleep=_none).refresh()


async def _sec_closes(db):
    for isin, cik, close in SEC:
        db.add(SecCompany(cik=cik, isin=isin, ticker=f"T{cik}"))
        db.add(
            CompanyPrice(cik=cik, date=date(2025, 11, 28), close=close, currency="USD")
        )
    db.add(
        ExchangeRate(
            date=date(2025, 11, 28),
            from_currency="USD",
            to_currency="EUR",
            rate=Decimal(str(USD_EUR)),
            source="frankfurter",
        )
    )
    await db.commit()


def _basket(sec_scale=1.01, price=261.06, units=900_000.0):
    rows = [
        ConstituentRow(
            line_no=i + 1,
            name=f"SEC {i}",
            weight_pct=Decimal("1"),
            isin=isin,
            price=Decimal(str(close * sec_scale)),
            units_held=Decimal("1000"),
        )
        for i, (isin, _, close) in enumerate(SEC)
    ]
    rows.append(
        ConstituentRow(
            line_no=9,
            name="SCHNEIDER ELECTRIC",
            weight_pct=Decimal("0.17"),
            isin=ISIN,
            price=Decimal(str(price)),
            units_held=Decimal(str(units)),
        )
    )
    return ParsedBasket(
        fund_isin=IWDA,
        as_of_date=AS_OF,
        source="blackrock",
        adapter="blackrock",
        rows=rows,
        source_rows=len(rows),
    )


def test_the_ishares_rows_carry_their_price_and_units():
    points = {
        "isin": {"formattedValue": ["FR0000121972", "US0378331005"]},
        "issueName": {"formattedValue": ["SCHNEIDER ELECTRIC", "APPLE"]},
        "holdingPercent": {"formattedValue": ["0.17", "5.95"]},
        "assetClass": {"formattedValue": ["Equity", "Equity"]},
        "unitPrice": {"formattedValue": ["261.06", "352.45"], "value": [261.06, "-"]},
        "unitsHeld": {"formattedValue": ["913,694.00", "1"], "value": [913694.0, 1.0]},
        "asOfDate": {"formattedValue": "18/Nov/2026"},
    }
    body = json.dumps(
        {
            "componentsByNameMap": {
                "holdings": {
                    "containersByNameMap": {"all": {"dataPointsByNameMap": points}}
                }
            }
        }
    ).encode()
    rows = parse_ishares(body, IWDA).rows
    assert rows[0].price == Decimal("261.06") and rows[0].units_held == Decimal(
        "913694.0"
    )
    assert rows[1].price is None  # not a number: absent, the basket still read


@pytest.mark.asyncio
async def test_a_european_company_gets_its_multiples_from_the_basket_price(db, dated):
    await _schneider(db)
    await _sec_closes(db)

    out = await record_constituent_prices(db, _basket(), "USD")
    await db.commit()
    assert out["verified"] is True and out["recorded"] == 1 and out["matches"] == 5
    # Asked again the same day: nothing doubled.
    assert (await record_constituent_prices(db, _basket(), "USD"))["recorded"] == 0

    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(
        await svc.company_by_key(f"lei:{LEI}"), "annual", "max"
    )
    close = 261.06 * USD_EUR  # the fund's USD price in Schneider's EUR
    assert page["series"]["price"]["points"][-1]["value"] == pytest.approx(close)
    assert page["key_stats"]["pe_ratio"] == pytest.approx(close / 7.53)


@pytest.mark.asyncio
async def test_a_wrong_price_currency_keeps_nothing(db, dated):
    await _schneider(db)
    await _sec_closes(db)
    # The SEC rows read 25% above their USD closes: the prices are not in USD.
    out = await record_constituent_prices(db, _basket(sec_scale=1.25), "USD")
    assert out["verified"] is False and out["recorded"] == 0
    assert out["median_ratio"] == pytest.approx(1.25) and "not in USD" in out["reason"]
    # No declaration, no prices.
    assert (await record_constituent_prices(db, _basket(), None))["skipped"]


@pytest.mark.asyncio
async def test_held_listings_closes_verify_a_basket_with_few_sec_filers(db, dated):
    """IEEM holds two SEC filers with a Yahoo close but held listings of Samsung, SK hynix,
    NU and TSMC: their daily closes, in their own currency, verify the declaration."""
    await _schneider(db)
    await _sec_closes(db)
    # Only two of the SEC filers keep a close in the window; three held listings stand in.
    for cik in (1002, 1003, 1004):
        await db.execute(
            CompanyPrice.__table__.delete().where(CompanyPrice.cik == cik)
        )
    for sid, (isin, _, close) in enumerate(SEC[2:], start=50):
        db.add(
            Security(
                id=sid, isin=isin, symbol=f"H{sid}", description="held", currency="EUR",
                conid=9500 + sid, asset_category="STK", exchange="XETRA",
            )
        )
        db.add(
            MarketPrice(
                security_id=sid,
                date=date(2025, 11, 28),
                close_price=Decimal(str(round(close * USD_EUR, 4))),
                currency="EUR",
                source="yahoo",
            )
        )
    await db.commit()
    out = await record_constituent_prices(db, _basket(), "USD")
    assert out["verified"] is True and out["matches"] == 5 and out["recorded"] == 1
    # A wrong declaration is still caught against the same closes.
    out = await record_constituent_prices(db, _basket(sec_scale=1.25), "USD")
    assert out["verified"] is False and "not in USD" in out["reason"]


@pytest.mark.asyncio
async def test_the_held_listing_of_the_companys_own_isin_comes_first(db, dated):
    await _schneider(db)
    await _sec_closes(db)
    await record_constituent_prices(db, _basket(), "USD")
    db.add(
        MarketPrice(
            security_id=1,
            date=date(2025, 11, 28),
            close_price=Decimal("250"),
            currency="EUR",
            source="yahoo",
        )
    )
    await db.commit()
    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(
        await svc.company_by_key(f"lei:{LEI}"), "annual", "max"
    )
    assert page["key_stats"]["pe_ratio"] == pytest.approx(250 / 7.53)


def _row(day, price, units, fund=IWDA):
    return ConstituentPrice(
        isin=ISIN,
        date=day,
        price=price,
        currency="USD",
        fund_isin=fund,
        units_held=units,
    )


def test_a_split_is_the_units_multiplying_as_the_price_divides():
    rows = [
        _row(date(2026, 1, 5), 200.0, 1000.0),
        _row(date(2026, 1, 6), 202.0, 1010.0),  # an ordinary day
        _row(date(2026, 1, 7), 101.5, 2020.0),  # 2-for-1
        _row(
            date(2026, 1, 8), 100.0, 2020.0, fund="IE00BKM4GZ66"
        ),  # another fund's first
    ]
    assert basket_splits(rows) == [(date(2026, 1, 7), 2.0)]
    # Units doubling while the price holds is a fund buying, not a split.
    assert (
        basket_splits(
            [
                _row(date(2026, 1, 5), 200.0, 1000.0),
                _row(date(2026, 1, 6), 199.0, 2000.0),
            ]
        )
        == []
    )
