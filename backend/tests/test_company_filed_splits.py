"""
A split the filings restated history for, which Yahoo never booked (review item CO1,
2026-11-08).

HON's newest 10-Q restated every earlier period for the 1:2 at its 2026 Aerospace
separation (EPS doubled, counts halved) while Yahoo's weekly closes were never adjusted, so
every P/E, P/S and P/FCF before it was half. The normaliser now emits the splits it votes,
the sync stores them with the rows, and `eps_basis` divides the closes before one Yahoo did
not book by its ratio. Pure, plus one DB round trip.
"""
from datetime import date, timedelta

import pytest

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.company_financials import SecCompany
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services.company_financials_normaliser import filed_splits, normalise
from app.services.company_financials_service import CompanyFinancialsService, eps_basis

Q2_25, Q3_25, Q2_26, Q3_26 = "hon-2025-q2", "hon-2025-q3", "hon-2026-q2", "hon-2026-q3"


def f(start, end, val, accn, filed):
    return {"start": start, "end": end, "val": val, "accn": accn, "filed": filed, "form": "10-Q"}


facts = {"cik": 773840, "entityName": "Honeywell", "facts": {"us-gaap": {
    # A year from a 10-K gives the fiscal calendar the quarters are labelled by.
    "Revenues": {"units": {"USD": [
        {**f("2025-01-01", "2025-12-31", 40e9, "hon-2025-k", "2026-02-13"), "form": "10-K"},
    ]}},
    "EarningsPerShareDiluted": {"units": {"USD/shares": [
        f("2025-04-01", "2025-06-30", 2.2, Q2_25, "2025-07-25"),
        f("2025-07-01", "2025-09-30", 2.3, Q3_25, "2025-10-24"),
        f("2026-04-01", "2026-06-30", 2.4, Q2_26, "2026-07-24"),
        f("2025-04-01", "2025-06-30", 2.2, Q2_26, "2026-07-24"),
        # The first filing after the separation: the comparative quarter on the new basis.
        f("2026-07-01", "2026-09-30", 4.0, Q3_26, "2026-10-23"),
        f("2025-07-01", "2025-09-30", 4.6, Q3_26, "2026-10-23"),
    ]}},
    "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
        f("2025-04-01", "2025-06-30", 650e6, Q2_25, "2025-07-25"),
        f("2025-07-01", "2025-09-30", 648e6, Q3_25, "2025-10-24"),
        f("2026-04-01", "2026-06-30", 645e6, Q2_26, "2026-07-24"),
        f("2025-04-01", "2025-06-30", 650e6, Q2_26, "2026-07-24"),
        f("2026-07-01", "2026-09-30", 322e6, Q3_26, "2026-10-23"),
        f("2025-07-01", "2025-09-30", 324e6, Q3_26, "2026-10-23"),
    ]}},
}}}
EVENT = {"after": "2026-07-24", "by": "2026-10-23", "ratio": 0.5}


def test_the_normaliser_emits_the_split_it_restated_for():
    result = normalise(facts)
    assert result.splits == [EVENT]
    eps = {(r["period_end"], r["metric"]): r["value"] for r in result.rows}
    assert eps[(date(2025, 6, 30), "eps_diluted")] == pytest.approx(4.4)


def test_a_single_filing_voted_off_both_ways_is_no_split():
    transient = [
        {"after": "2024-01-01", "by": "2024-04-01", "ratio": 2.0},
        {"after": "2024-04-01", "by": "2024-07-01", "ratio": 0.5},
    ]
    assert filed_splits(transient) == []
    assert filed_splits([EVENT]) == [EVENT]


def _weekly(first, last, close):
    out, d = [], first
    while d <= last:
        out.append((d, close))
        d += timedelta(days=7)
    return out


PRICES = _weekly(date(2026, 6, 5), date(2026, 11, 6), 200.0)


def test_closes_before_an_unbooked_split_move_to_the_figures_basis():
    shown = dict(eps_basis(PRICES, [], date(2026, 10, 23), [EVENT]))
    assert shown[date(2026, 7, 24)] == 400.0  # the old basis, doubled
    assert shown[date(2026, 10, 30)] == 200.0  # the new basis, as quoted
    # Between the last old-basis filing and the first new one: either basis, so absent.
    assert date(2026, 9, 4) not in shown and date(2026, 10, 23) not in shown


def test_a_split_yahoo_booked_is_in_its_closes_already():
    booked = [(date(2026, 9, 15), 0.5)]
    assert eps_basis(PRICES, booked, date(2026, 10, 23), [EVENT]) == PRICES
    # A different split years before is not this one.
    elsewhere = [(date(2019, 3, 1), 0.5)]
    assert eps_basis(PRICES, elsewhere, date(2026, 10, 23), [EVENT]) != PRICES


@pytest.mark.asyncio
async def test_the_sync_stores_them_and_the_overview_reads_them(db):
    repo = CompanyFinancialsRepository(db)
    result = normalise(facts)
    await repo.replace_rows(773840, result.rows, utcnow(), result.entity_name, result.splits)
    await db.commit()
    assert (await db.get(SecCompany, 773840)).filed_splits is not None

    service = CompanyFinancialsService(db)
    rows = await repo.rows_for(773840)
    shown = dict(await service._on_eps_basis(773840, rows, PRICES))
    assert shown[date(2026, 6, 5)] == 400.0

    # A fetch whose filings restate nothing clears them.
    await repo.replace_rows(773840, result.rows, utcnow(), None, [])
    await db.commit()
    assert (await db.get(SecCompany, 773840)).filed_splits is None
