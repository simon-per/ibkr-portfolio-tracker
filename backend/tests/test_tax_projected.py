"""
The current year's dividends through 31 December on the tax report (GitHub issue #77).

The expected part is the dividend forecast's own year row -- `growth.annual`'s
`forecast_net_eur` and `forecast_withholding_eur` -- never a second projection, so the
agreement test reads both sides from their own services. `projected` exists for the
current year only. EUR base, synthetic book, offline.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

import app.services.tax_service as tax_module
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.services.dividend_service import DividendService
from app.services.tax_service import TaxService

TODAY = date(2026, 5, 1)
YEAR = TODAY.year
QUARTERLY = (date(2025, 10, 15), date(2026, 1, 15), date(2026, 4, 15))


@pytest.fixture
def pinned_today(monkeypatch):
    """The report's clock, so the current year and the forecast horizon are fixed."""
    monkeypatch.setattr(tax_module, "utc_today", lambda: TODAY)


async def _security(db) -> None:
    db.add(Security(id=1, isin="US0000000001", symbol="AAA", description="Alpha",
                    currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    await db.flush()


async def _paid(db, on: date, gross: str, wht: str, source: str = "ibkr") -> None:
    await DividendRepository(db).upsert_payment({
        "security_id": 1, "ex_date": on, "pay_date": on, "currency": "EUR",
        "shares_held": Decimal("0") if source == "ibkr" else Decimal("10"),
        "gross_amount_eur": Decimal(gross), "withholding_tax_eur": Decimal(wht),
        "net_amount_eur": Decimal(gross) - Decimal(wht), "source": source,
        "last_computed": datetime(2026, 1, 1),
    })


async def _quarterly_payer(db) -> None:
    """10 shares of a quarterly payer: Yahoo's per-share history plus IBKR's payments."""
    await _security(db)
    db.add(TaxLot(security_id=1, open_date=date(2025, 1, 2), quantity=Decimal("10"),
                  cost_basis=Decimal("100"), price_per_unit=Decimal("10"), currency="EUR",
                  cost_basis_eur=Decimal("100"), is_open=True))
    repo = DividendRepository(db)
    for d in QUARTERLY:
        await repo.upsert_payment({
            "security_id": 1, "ex_date": d, "pay_date": d, "currency": "EUR",
            "amount_per_share": Decimal("0.50"), "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("0"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("0"), "source": "yfinance_estimate",
        })
        await repo.upsert_payment({
            "security_id": 1, "ex_date": d + timedelta(days=20),
            "pay_date": d + timedelta(days=20), "currency": "EUR",
            "shares_held": Decimal("0"), "gross_amount_eur": Decimal("5.00"),
            "withholding_tax_eur": Decimal("0.75"), "net_amount_eur": Decimal("4.25"),
            "source": "ibkr", "per_share_native": Decimal("0.50"),
            "dividend_kind": "ordinary",
        })
    await db.commit()


def _stub_forecast(monkeypatch, rows):
    async def breakdown(self, *args, **kwargs):
        return {"growth": {"annual": rows}}
    monkeypatch.setattr(DividendService, "get_dividend_breakdown", breakdown)


@pytest.mark.asyncio
async def test_the_projection_is_the_dividend_forecasts_own_year_row(db, pinned_today):
    await _quarterly_payer(db)
    report = await TaxService(db).get_tax_report(YEAR)
    projected = report["projected"]

    breakdown = await DividendService(db).get_dividend_breakdown(year=YEAR, as_of=TODAY)
    (row,) = [r for r in breakdown["growth"]["annual"] if r["year"] == YEAR]
    assert row["forecast_net_eur"] > 0  # the fixture really does project something
    expected_gross = row["forecast_net_eur"] + row["forecast_withholding_eur"]
    assert projected["through"] == f"{YEAR}-12-31"
    assert projected["expected_gross"] == pytest.approx(expected_gross, abs=0.01)
    assert projected["expected_withholding"] == row["forecast_withholding_eur"]
    totals = report["dividend_totals"]
    assert projected["gross"] == pytest.approx(totals["gross"] + expected_gross, abs=0.01)
    assert projected["withholding"] == pytest.approx(
        totals["withholding"] + row["forecast_withholding_eur"], abs=0.01
    )


@pytest.mark.asyncio
async def test_received_plus_expected(db, pinned_today, monkeypatch):
    await _security(db)
    await _paid(db, date(YEAR, 3, 2), "100", "15")
    await db.commit()
    _stub_forecast(monkeypatch, [
        {"year": YEAR - 1, "forecast_net_eur": 999.0, "forecast_withholding_eur": 99.0},
        {"year": YEAR, "forecast_net_eur": 85.0, "forecast_withholding_eur": 15.0},
    ])
    service = TaxService(db)
    report = await service.get_tax_report(YEAR)
    assert report["projected"] == {
        "through": f"{YEAR}-12-31",
        "expected_gross": 100.0,
        "expected_withholding": 15.0,
        "gross": 200.0,
        "withholding": 30.0,
    }
    # The CSV's dividend table gains the row, in its own Gross and Withholding columns.
    assert f"THROUGH {YEAR}-12-31,,,,200.0,30.0,,est." in service.to_csv(report).splitlines()


@pytest.mark.asyncio
async def test_nothing_forecast_is_a_real_zero(db, pinned_today, monkeypatch):
    await _security(db)
    await _paid(db, date(YEAR, 3, 2), "100", "15")
    await db.commit()
    _stub_forecast(monkeypatch, [])
    projected = (await TaxService(db).get_tax_report(YEAR))["projected"]
    assert (projected["expected_gross"], projected["gross"]) == (0.0, 100.0)
    assert projected["withholding"] == 15.0


@pytest.mark.asyncio
async def test_no_recorded_withholding_means_none_through_year_end(db, pinned_today, monkeypatch):
    """Every received row an estimate: the withholding total is absent, so is this one."""
    await _security(db)
    await _paid(db, date(YEAR, 3, 2), "40", "0", source="yfinance_estimate")
    await db.commit()
    _stub_forecast(monkeypatch, [
        {"year": YEAR, "forecast_net_eur": 85.0, "forecast_withholding_eur": 15.0},
    ])
    service = TaxService(db)
    report = await service.get_tax_report(YEAR)
    assert report["dividend_totals"]["withholding"] is None
    assert report["projected"]["gross"] == 140.0
    assert report["projected"]["withholding"] is None
    assert f"THROUGH {YEAR}-12-31,,,,140.0,,,est." in service.to_csv(report).splitlines()


@pytest.mark.asyncio
async def test_a_past_year_has_no_projection_and_asks_no_forecast(db, pinned_today, monkeypatch):
    await _security(db)
    await _paid(db, date(YEAR - 1, 3, 2), "100", "15")
    await db.commit()

    async def must_not_run(self, *args, **kwargs):
        raise AssertionError("a past year asked for a forecast")

    monkeypatch.setattr(DividendService, "get_dividend_breakdown", must_not_run)
    service = TaxService(db)
    report = await service.get_tax_report(YEAR - 1)
    assert report["projected"] is None
    assert report["warnings"] == []
    assert not any(line.startswith("THROUGH") for line in service.to_csv(report).splitlines())


@pytest.mark.asyncio
async def test_a_failing_forecast_is_a_warning_not_a_500(db, pinned_today, monkeypatch):
    await _security(db)
    await _paid(db, date(YEAR, 3, 2), "100", "15")
    await db.commit()

    async def broken(self, *args, **kwargs):
        raise RuntimeError("forecast failed")

    monkeypatch.setattr(DividendService, "get_dividend_breakdown", broken)
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["projected"] is None
    assert any("could not be computed" in w and f"{YEAR}-12-31" in w
               for w in report["warnings"])
    # The filing figures beside it are untouched.
    assert report["dividend_totals"]["gross"] == 100.0
