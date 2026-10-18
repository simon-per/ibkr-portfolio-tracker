"""
`/api/dividends/summary` returns exactly what its response_model declares.

A `response_model` is a filter, not an assertion: FastAPI drops any key the model does
not declare and reports nothing. So the model is only a safety net while it stays in
step with the service — the moment the service grows a key the model lacks, that key
disappears from the wire and the only symptom is a blank spot in the UI.

That is not hypothetical here. `DividendSummaryResponse` declared five of the service's
ten keys while the route had no `response_model` at all; the undeclared ones (`source`,
`ibkr_from`, `total_withholding_eur`, ...) are exactly the provenance fields that stop
the Performance tab's *Dividend Income* card presenting IBKR actuals net of real
withholding as though they were gross. Attaching the model without completing it first
would have shipped that blanking as a "hardening" change.

So this compares the two key sets directly rather than spot-checking names.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.schemas.portfolio import DividendSummaryResponse
from app.services.dividend_service import DividendService
from tests.memory_db import memory_engine

# Added by the route after the service returns, so it is legitimately absent from the
# service dict while still belonging to the response.
ROUTE_SUPPLIED_KEYS = {"sync_in_progress"}


async def _make_session():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="NL0010273215", symbol="ASML", description="ASML Holding",
                         currency="EUR", conid=100, asset_category="STK", exchange="AEB"))
    session.add(TaxLot(
        security_id=1, open_date=date(2025, 1, 6), quantity=Decimal("10"),
        cost_basis=Decimal("6000"), cost_basis_eur=Decimal("6000"),
        price_per_unit=Decimal("600"), currency="EUR", is_open=True,
    ))
    await session.flush()
    return engine, session


async def _seed_both_eras(session):
    """One row per era, so the splice and the three-way source flag both engage."""
    repo = DividendRepository(session)
    await repo.upsert_payment({
        "security_id": 1, "ex_date": date(2025, 2, 14), "pay_date": date(2025, 2, 14),
        "currency": "EUR", "shares_held": Decimal("10"),
        "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
    })
    await repo.upsert_payment({
        "security_id": 1, "ex_date": date(2026, 2, 11), "pay_date": date(2026, 2, 11),
        "currency": "EUR", "shares_held": Decimal("0"),
        "gross_amount_eur": Decimal("20"), "withholding_tax_eur": Decimal("3"),
        "net_amount_eur": Decimal("17"), "source": "ibkr",
    })


@pytest.mark.asyncio
async def test_the_model_declares_every_key_the_service_returns():
    """The failure this endpoint actually had: a key on the wire, absent from the model."""
    engine, session = await _make_session()
    try:
        await _seed_both_eras(session)
        returned = set((await DividendService(session).get_dividend_summary()).keys())
        declared = set(DividendSummaryResponse.model_fields.keys())

        undeclared = returned - declared
        assert not undeclared, (
            f"{sorted(undeclared)} are returned by DividendService.get_dividend_summary() "
            f"but not declared on DividendSummaryResponse, so the response_model will "
            f"silently drop them. Add them to the model."
        )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_model_declares_nothing_the_service_does_not_supply():
    """
    The mirror image, which fails quietly in a different way: a declared-but-unsupplied
    field falls back to its default, so the card renders a plausible wrong value (0.00
    withholding, "yfinance_estimate" provenance) rather than an error.
    """
    engine, session = await _make_session()
    try:
        await _seed_both_eras(session)
        returned = set((await DividendService(session).get_dividend_summary()).keys())
        declared = set(DividendSummaryResponse.model_fields.keys())

        unsupplied = declared - returned - ROUTE_SUPPLIED_KEYS
        assert not unsupplied, (
            f"{sorted(unsupplied)} are declared on DividendSummaryResponse but never "
            f"returned by the service, so they silently take their default value."
        )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_service_output_validates_against_the_model():
    """
    Matching key names is not enough — a type that cannot coerce would 500 at response
    time, which no service-level test would catch.
    """
    engine, session = await _make_session()
    try:
        await _seed_both_eras(session)
        payload = await DividendService(session).get_dividend_summary()
        payload["sync_in_progress"] = False

        model = DividendSummaryResponse(**payload)

        # The splice keeps the 2025 estimate and the 2026 actual: the boundary era.
        assert model.source == "mixed"
        assert model.ibkr_from == "2026-02-11"
        # NET, and the back-compat key agrees with the explicit one. The 2025 estimate
        # reads net of the 15% IBKR withheld on this security (issue #61): 12.75 + 17,
        # and its 2.25 joins the withholding so gross = net + withholding.
        assert model.total_eur == model.total_net_eur == pytest.approx(29.75)
        assert model.total_gross_eur == pytest.approx(35.0)
        assert model.total_withholding_eur == pytest.approx(5.25)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_rejects_a_value_outside_the_three_way_flag():
    """
    `source` is a Literal because a fourth value would reach the UI's footnote logic and
    fall through to the estimate wording — claiming a gross guess over real withholding.
    """
    engine, session = await _make_session()
    try:
        await _seed_both_eras(session)
        payload = await DividendService(session).get_dividend_summary()
        payload["sync_in_progress"] = False
        payload["source"] = "ibkr_partial"

        with pytest.raises(ValueError):
            DividendSummaryResponse(**payload)
    finally:
        await session.close()
        await engine.dispose()
