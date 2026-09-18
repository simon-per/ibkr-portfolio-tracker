"""
`/api/dividends/breakdown` returns exactly what its response models declare.

`tests/test_dividend_summary_contract.py` pins one endpoint's key set for a reason that
generalises, and this is the endpoint where it generalises most: a `response_model` is a
**filter**, so a key the service returns and the model does not declare vanishes from the
wire and reports nothing. The summary had five of ten keys undeclared before anyone
noticed.

The breakdown is the largest response in the app — nine top-level keys over **eight**
nested models — and it had no such test. `test_api_smoke.py` spot-checks names it was
told to look for, which catches a field someone remembered to think about and nothing
else. So this walks every model the payload actually populates and compares both
directions, which is the only form that covers a field added next year.

The two directions fail differently, and neither raises:

- **undeclared** — the service supplies it, the model drops it, the UI reads `undefined`.
- **unsupplied** — the model declares it, the service never sends it, so it silently
  takes its default. `0.00` withholding and `yfinance_estimate` provenance are both
  plausible-looking defaults, which is what makes this the worse direction.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.schemas.portfolio import (
    DividendAnnualRow,
    DividendBreakdownResponse,
    DividendDelta,
    DividendForwardYield,
    DividendGrowth,
    DividendLatestMonth,
    DividendMonthBar,
    DividendSecurityRow,
    DividendUpcomingPayment,
)
from app.services.dividend_service import DividendService

AS_OF = date(2026, 5, 1)


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="NL0010273215", symbol="ASML", description="ASML Holding",
                         currency="EUR", conid=100, asset_category="STK", exchange="AEB"))
    await session.flush()
    return engine, session


async def _seed(session):
    """
    A payload that populates every nested model at once.

    Each piece is load-bearing: the two eras engage the splice and the three-way source
    flag; four quarterly payments give the forecast a cadence (so `upcoming` and
    `latest_month` are non-empty); the price is what makes `forward_yield` and the
    per-row yields resolve at all — and it must be dated **today**, because
    `get_positions_breakdown()` prices at `date.today()` rather than at the injected
    `as_of`, with a 14-day lookback.
    """
    session.add(TaxLot(
        security_id=1, open_date=date(2024, 6, 3), quantity=Decimal("10"),
        cost_basis=Decimal("6000"), cost_basis_eur=Decimal("6000"),
        price_per_unit=Decimal("600"), currency="EUR", is_open=True,
    ))
    session.add(MarketPrice(
        security_id=1, date=date.today(), close_price=Decimal("800"),
        currency="EUR", source="test",
    ))
    await session.flush()

    repo = DividendRepository(session)
    # Pre-IBKR era, so the splice and `ibkr_from` engage.
    await repo.upsert_payment({
        "security_id": 1, "ex_date": date(2025, 2, 14), "pay_date": date(2025, 2, 14),
        "currency": "EUR", "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
        "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
    })
    # IBKR era, quarterly, so a cadence can be inferred and projected forward.
    for d in (date(2025, 8, 11), date(2025, 11, 11),
              date(2026, 2, 11), date(2026, 4, 11)):
        await repo.upsert_payment({
            "security_id": 1, "ex_date": d, "pay_date": d,
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("20"), "withholding_tax_eur": Decimal("3"),
            "net_amount_eur": Decimal("17"), "source": "ibkr",
        })
    await session.commit()


async def _payload(session):
    """Seeds and returns the populated payload. The seeding is not optional — an empty
    payload compares clean against every model, so a test that forgot it would pass
    while checking nothing. That happened while writing this file."""
    await _seed(session)
    return await DividendService(session).get_dividend_breakdown(
        include_forecast=True, as_of=AS_OF,
    )


def _pairs(payload):
    """
    Every (label, dict, model) the payload populates, flattened.

    Built from the payload rather than from a hand-kept list, so a nested model that
    starts being returned is compared the day it appears. Anything that comes back
    `None` is asserted present by `test_the_fixture_populates_every_nested_model` — a
    contract test that silently skipped half the response would be worse than none.
    """
    growth = payload["growth"]
    out = [
        ("DividendBreakdownResponse", payload, DividendBreakdownResponse),
        ("DividendGrowth", growth, DividendGrowth),
        ("DividendForwardYield", payload["forward_yield"], DividendForwardYield),
        ("DividendLatestMonth", growth["latest_month"], DividendLatestMonth),
    ]
    for key in ("ttm", "ytd", "avg_month"):
        out.append((f"DividendDelta ({key})", growth[key], DividendDelta))
    for label, rows, model in (
        ("DividendMonthBar", payload["months"], DividendMonthBar),
        ("DividendSecurityRow", payload["securities"], DividendSecurityRow),
        ("DividendUpcomingPayment", payload["upcoming"], DividendUpcomingPayment),
        ("DividendAnnualRow", growth["annual"], DividendAnnualRow),
    ):
        for i, row in enumerate(rows):
            out.append((f"{label}[{i}]", row, model))
    return [(label, d, m) for label, d, m in out if d is not None]


@pytest.mark.asyncio
async def test_the_fixture_populates_every_nested_model():
    """
    The guard on the guard. `_pairs` drops anything `None`, so a fixture that stopped
    producing a forecast would quietly reduce this file to checking the top level.
    """
    engine, session = await _make_session()
    try:
        payload = await _payload(session)
        assert payload["forward_yield"] is not None
        assert payload["growth"]["latest_month"] is not None
        assert payload["growth"]["annual"], "no annual rows"
        assert payload["months"], "no month bars"
        assert any(m["ttm_net_eur"] is not None for m in payload["months"]), "no completed TTM window"
        assert payload["securities"], "no security rows"
        assert payload["upcoming"], "no projected payments"
        # One of each provenance, or the splice is not being exercised.
        assert payload["ibkr_from"] is not None
        assert len(_pairs(payload)) >= 12
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_every_model_declares_every_key_its_service_dict_returns():
    """The failure the summary endpoint actually had: a key on the wire, dropped silently."""
    engine, session = await _make_session()
    try:
        payload = await _payload(session)
        for label, supplied, model in _pairs(payload):
            undeclared = set(supplied) - set(model.model_fields)
            assert not undeclared, (
                f"{label}: {sorted(undeclared)} is returned by DividendService but not "
                f"declared on {model.__name__}, so response_model drops it and the "
                f"client reads undefined. Add it to the model."
            )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_model_declares_a_key_the_service_never_supplies():
    """
    The mirror image, and the worse one: an unsupplied field takes its default, so the
    UI renders a plausible wrong value instead of failing.
    """
    engine, session = await _make_session()
    try:
        payload = await _payload(session)
        for label, supplied, model in _pairs(payload):
            unsupplied = set(model.model_fields) - set(supplied)
            assert not unsupplied, (
                f"{label}: {sorted(unsupplied)} is declared on {model.__name__} but "
                f"never returned by the service, so it silently takes its default."
            )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_whole_payload_validates_and_survives_serialization():
    """
    Matching names is not enough: a type that cannot coerce 500s at response time, which
    no service-level test sees. And round-tripping proves the filter keeps what it was
    given rather than merely accepting it.
    """
    engine, session = await _make_session()
    try:
        payload = await _payload(session)
        model = DividendBreakdownResponse(**payload)
        dumped = model.model_dump()

        assert set(dumped) == set(payload)
        assert set(dumped["growth"]) == set(payload["growth"])
        assert set(dumped["forward_yield"]) == set(payload["forward_yield"])
        assert set(dumped["securities"][0]) == set(payload["securities"][0])

        # The figures the models exist to protect, through the serializer.
        assert model.forward_yield.pct == payload["forward_yield"]["pct"]
        assert model.forward_yield.basis in ("net", "mixed", "gross_estimate")
        assert model.securities[0].forward_yield_pct is not None
        assert model.growth.ttm.net_eur == payload["growth"]["ttm"]["net_eur"]
        assert [m.model_dump() for m in model.months] == payload["months"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_forecastless_payload_still_matches_its_models():
    """
    `?forecast=false` takes a different branch — `forward_yield` becomes None and the
    projection fields zero — and a contract checked only on the populated path would
    miss a key that exists on one branch and not the other.
    """
    engine, session = await _make_session()
    try:
        await _seed(session)
        payload = await DividendService(session).get_dividend_breakdown(
            include_forecast=False, as_of=AS_OF,
        )
        assert payload["forward_yield"] is None
        for label, supplied, model in _pairs(payload):
            assert set(supplied) == set(model.model_fields), (
                f"{label} differs from {model.__name__} on the forecast=false branch: "
                f"extra={sorted(set(supplied) - set(model.model_fields))} "
                f"missing={sorted(set(model.model_fields) - set(supplied))}"
            )
        DividendBreakdownResponse(**payload)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_projection_is_not_a_hundred_percent_decline():
    """
    Production served `next_12m_vs_ttm_pct: -100.0` on ?forecast=false: a zero numerator
    over a real trailing base, i.e. "dividends will fall 100%" manufactured by a flag
    rather than measured from anything. `_pct` is right to guard only a zero *base* — a
    quarterly payer has empty months constantly — so the caller has to withhold the
    comparison when there is no projection to compare.
    """
    engine, session = await _make_session()
    try:
        await _seed(session)
        svc = DividendService(session)
        off = await svc.get_dividend_breakdown(include_forecast=False, as_of=AS_OF)
        assert off["growth"]["ttm"]["net_eur"] > 0          # a real base existed
        assert off["growth"]["next_12m_vs_ttm_pct"] is None

        on = await svc.get_dividend_breakdown(include_forecast=True, as_of=AS_OF)
        assert on["growth"]["next_12m_vs_ttm_pct"] is not None
    finally:
        await session.close()
        await engine.dispose()
