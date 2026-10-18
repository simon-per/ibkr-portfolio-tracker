"""
DA-1 credits only the treaty residual rate (GitHub issue #62).

The by-country table summed the raw withholding and labelled it DA-1, so a country that
withholds above its treaty rate -- Korea's 22% against 15% -- overstated the claim by the
difference, which has to be reclaimed from the source country instead. Each country row
now splits its recorded withholding per payment into `creditable = min(withheld, gross x
residual)` and `excess = withheld - creditable`, and is keyed on the country IBKR's
withholding line names rather than on the ISIN prefix, which is wrong for an ADR.

EUR base, so no FX. Offline: no network.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.services.dividend_service import DividendService, parse_ibkr_withholding_country
from app.services.swiss_treaty_rates import (
    RESIDUAL_DIVIDEND_RATE_PCT,
    creditable_split,
    residual_rate_pct,
    withholding_country,
)
from app.services.tax_service import TaxService

YEAR = 2025


# --- the table and its arithmetic ------------------------------------------------------

def test_the_treaty_table_is_iso_codes_and_plausible_rates():
    assert len(RESIDUAL_DIVIDEND_RATE_PCT) >= 8
    for country, rate in RESIDUAL_DIVIDEND_RATE_PCT.items():
        assert len(country) == 2 and country.isupper(), country
        assert isinstance(rate, Decimal) and Decimal("0") <= rate < Decimal("35"), country
    assert residual_rate_pct("US") == Decimal("15")
    assert residual_rate_pct("kr") == Decimal("15")
    assert residual_rate_pct("JP") == Decimal("10")


def test_switzerland_has_no_residual_rate():
    """Swiss withholding is refunded through the securities statement, never DA-1."""
    assert residual_rate_pct("CH") is None
    assert residual_rate_pct(None) is None
    assert residual_rate_pct("??") is None


def test_withholding_above_the_treaty_rate_splits_into_creditable_and_excess():
    assert creditable_split(Decimal("100"), Decimal("22"), Decimal("15")) == (
        Decimal("15"), Decimal("7"))


def test_withholding_at_or_below_the_treaty_rate_is_all_creditable():
    assert creditable_split(Decimal("100"), Decimal("15"), Decimal("15")) == (
        Decimal("15"), Decimal("0"))
    assert creditable_split(Decimal("100"), Decimal("10"), Decimal("15")) == (
        Decimal("10"), Decimal("0"))


def test_no_rate_means_no_split_unless_nothing_was_withheld():
    assert creditable_split(Decimal("100"), Decimal("21"), None) is None
    # Nothing withheld is nothing to credit, whatever the treaty says.
    assert creditable_split(Decimal("100"), Decimal("0"), None) == (Decimal("0"), Decimal("0"))


def test_the_stated_withholding_country_wins_over_the_isin():
    assert withholding_country("TW", "US8740391003") == "TW"
    assert withholding_country(None, "US8740391003") == "US"
    assert withholding_country(None, None) is None


# --- reading it off the statement ----------------------------------------------------------

def test_the_country_is_read_off_ibkrs_withholding_line():
    line = "VT(US9220427424) CASH DIVIDEND USD 0.4084 PER SHARE - US TAX"
    assert parse_ibkr_withholding_country(line) == "US"
    assert parse_ibkr_withholding_country(
        "TSM(US8740391003) Cash Dividend USD 0.70 per Share - TW Tax") == "TW"
    # A dividend line names no country, and neither does a free-text one.
    assert parse_ibkr_withholding_country(
        "VT(US9220427424) CASH DIVIDEND USD 0.4084 PER SHARE (Ordinary Dividend)") is None
    assert parse_ibkr_withholding_country("GOOGL WITHHOLDING @ 15%") is None
    assert parse_ibkr_withholding_country(None) is None


def _ct(ct_type, amount, description, pay_date=date(YEAR, 5, 2)):
    return {
        "ib_key": f"{ct_type}-{amount}-{description}", "conid": "100", "symbol": "TSM",
        "pay_date": pay_date, "type": ct_type, "amount": Decimal(str(amount)),
        "currency": "EUR", "description": description,
    }


async def _adr(db) -> None:
    db.add(Security(id=1, isin="US8740391003", symbol="TSM", description="TSMC ADR",
                    currency="EUR", conid=100, asset_category="STK", exchange="NYSE"))
    await db.flush()


@pytest.mark.asyncio
async def test_the_ingest_stores_the_withholding_country(db):
    await _adr(db)
    line = "TSM(US8740391003) CASH DIVIDEND EUR 0.70 PER SHARE"
    await DividendService(db).sync_dividends_from_cash_transactions([
        _ct("DIVIDEND", 70, line + " (Ordinary Dividend)"),
        _ct("WHTAX", -14.70, line + " - TW TAX"),
    ], {"100": 1})
    (dp,) = await DividendService(db).repo.get_by_security(1)
    assert dp.withholding_country == "TW"


@pytest.mark.asyncio
async def test_two_countries_on_one_payment_store_none_rather_than_a_pick(db):
    await _adr(db)
    line = "TSM(US8740391003) CASH DIVIDEND EUR 0.70 PER SHARE"
    await DividendService(db).sync_dividends_from_cash_transactions([
        _ct("DIVIDEND", 70, line + " (Ordinary Dividend)"),
        _ct("WHTAX", -10, line + " - TW TAX"),
        _ct("WHTAX", -4.70, line + " - US TAX"),
    ], {"100": 1})
    (dp,) = await DividendService(db).repo.get_by_security(1)
    assert dp.withholding_country is None


@pytest.mark.asyncio
async def test_a_reingest_backfills_a_row_stored_before_the_column(db):
    """`app.cli.ingest_flex_xml` re-runs this same upsert; it must fill, not skip."""
    await _adr(db)
    line = "TSM(US8740391003) CASH DIVIDEND EUR 0.70 PER SHARE"
    txns = [_ct("DIVIDEND", 70, line + " (Ordinary Dividend)"),
            _ct("WHTAX", -14.70, line + " - TW TAX")]
    svc = DividendService(db)
    await svc.sync_dividends_from_cash_transactions(txns, {"100": 1})
    (dp,) = await svc.repo.get_by_security(1)
    dp.withholding_country = None          # as every row ingested before 2026-10-18
    await db.flush()

    await svc.sync_dividends_from_cash_transactions(txns, {"100": 1})
    (dp,) = await svc.repo.get_by_security(1)
    assert dp.withholding_country == "TW"
    assert dp.withholding_tax_eur == Decimal("14.70")     # nothing else moved


# --- the report ------------------------------------------------------------------------------

def _pay(sid, day, gross, wht, *, country=None, source="ibkr"):
    estimated = source != "ibkr"
    return DividendPayment(
        security_id=sid, ex_date=date(YEAR, 1, day), pay_date=date(YEAR, 1, day),
        currency="EUR", shares_held=Decimal("0"),
        gross_amount_eur=Decimal(gross),
        withholding_tax_eur=Decimal("0") if estimated else Decimal(wht),
        net_amount_eur=Decimal(gross) - (Decimal("0") if estimated else Decimal(wht)),
        source=source, withholding_country=country,
    )


async def _book(db) -> None:
    db.add_all([
        Security(id=1, isin="KR7000660001", symbol="000660", description="SK hynix",
                 currency="EUR", conid=1, exchange="KSE"),
        Security(id=2, isin="US0000000002", symbol="USA", description="US Co",
                 currency="EUR", conid=2, exchange="NYSE"),
        Security(id=3, isin="US8740391003", symbol="TSM", description="TSMC ADR",
                 currency="EUR", conid=3, exchange="NYSE"),
        Security(id=4, isin="CH0000000004", symbol="SWI", description="Swiss Co",
                 currency="EUR", conid=4, exchange="SIX"),
        Security(id=5, isin="DE0000000005", symbol="EST", description="German Co",
                 currency="EUR", conid=5, exchange="XETRA"),
    ])
    await db.flush()
    db.add_all([
        # Korea withholds 22% against a 15% residual, on two payments.
        _pay(1, 10, "100", "22", country="KR"),
        _pay(1, 11, "200", "44", country="KR"),
        # The US at its treaty rate, and once below it.
        _pay(2, 12, "100", "15", country="US"),
        _pay(2, 13, "100", "10"),                      # no stated country: the ISIN's
        # A Taiwanese ADR: US ISIN, withheld by Taiwan, which is not in the table.
        _pay(3, 14, "100", "21", country="TW"),
        # Switzerland withheld nothing here: a zero needs no rate.
        _pay(4, 15, "100", "0"),
        # An estimate-only country: no recorded withholding, so nothing to split. Dated
        # before the first IBKR payment, where the era splice keeps estimates.
        _pay(5, 5, "50", "0", source="yfinance_estimate"),
    ])
    await db.commit()


def _by_country(report):
    return {row["country"]: row for row in report["dividend_by_country"]}


@pytest.mark.asyncio
async def test_a_country_withheld_above_its_treaty_rate_shows_the_split(db):
    await _book(db)
    kr = _by_country(await TaxService(db).get_tax_report(YEAR))["KR"]
    assert kr["withholding"] == 66.0
    assert kr["rate_pct"] == 22.0
    assert kr["residual_pct"] == 15.0
    assert kr["creditable"] == 45.0
    assert kr["excess"] == 21.0


@pytest.mark.asyncio
async def test_a_country_at_or_below_its_treaty_rate_has_no_excess(db):
    await _book(db)
    us = _by_country(await TaxService(db).get_tax_report(YEAR))["US"]
    assert (us["withholding"], us["creditable"], us["excess"]) == (25.0, 25.0, 0.0)
    assert us["rate_pct"] == 12.5


@pytest.mark.asyncio
async def test_the_split_is_per_payment_not_on_the_country_sums(db):
    """min() does not add: 30% and 0% on equal grosses is not 15% on both."""
    db.add(Security(id=1, isin="KR7000660001", symbol="000660", description="SK hynix",
                    currency="EUR", conid=1, exchange="KSE"))
    await db.flush()
    db.add_all([_pay(1, 10, "100", "30", country="KR"), _pay(1, 11, "100", "0", country="KR")])
    await db.commit()
    kr = _by_country(await TaxService(db).get_tax_report(YEAR))["KR"]
    assert (kr["creditable"], kr["excess"]) == (15.0, 15.0)


@pytest.mark.asyncio
async def test_the_stated_country_keys_the_row(db):
    # An unlisted country's row is not split: residual_rate_pct("??") / creditable_split above.
    await _book(db)
    rows = _by_country(await TaxService(db).get_tax_report(YEAR))
    tw = rows["TW"]
    assert tw["withholding"] == 21.0 and tw["rate_pct"] == 21.0
    assert (tw["residual_pct"], tw["creditable"], tw["excess"]) == (15.0, 15.0, 6.0)
    # The ADR's withholding is not filed under its ISIN's country.
    assert rows["US"]["payments"] == 2


@pytest.mark.asyncio
async def test_nothing_withheld_splits_to_zero_without_a_rate(db):
    await _book(db)
    ch = _by_country(await TaxService(db).get_tax_report(YEAR))["CH"]
    assert ch["residual_pct"] is None
    assert (ch["withholding"], ch["creditable"], ch["excess"]) == (0.0, 0.0, 0.0)


@pytest.mark.asyncio
async def test_an_estimate_only_country_stays_absent(db):
    await _book(db)
    de = _by_country(await TaxService(db).get_tax_report(YEAR))["DE"]
    assert de["withholding"] is None
    assert (de["rate_pct"], de["creditable"], de["excess"]) == (None, None, None)
    # The rate is a fact about the treaty, not about the payments.
    assert de["residual_pct"] == 15.0


@pytest.mark.asyncio
async def test_the_csv_carries_the_split(db):
    await _book(db)
    service = TaxService(db)
    lines = service.to_csv(await service.get_tax_report(YEAR)).splitlines()
    header = next(l for l in lines if l.startswith("Country,"))
    assert header == ("Country,Positions,Gross (EUR),Withholding (EUR),Rate (%),"
                      "Treaty rate (%),Creditable (EUR),Excess (EUR),Net (EUR),"
                      "Estimated payments")
    assert next(l for l in lines if l.startswith("KR,")) == "KR,1,300.0,66.0,22.0,15.0,45.0,21.0,234.0,"
    assert next(l for l in lines if l.startswith("TW,")) == "TW,1,100.0,21.0,21.0,15.0,15.0,6.0,79.0,"
