"""
An estimate's net is its gross less the ladder withholding (GitHub issue #61).

`compute_dividend_income` stores ``net = gross`` on a `yfinance_estimate` row, because
yfinance knows no withholding. `get_dividend_breakdown` added that as NET to the
months, the years and TTM/YTD, and then added the ladder-rate withholding for the same
years beside it — so on an estimate-era year "net" plus the tax line exceeded gross, and
every comparison across the era splice measured IBKR's real net against estimated
gross, understating growth by about the withholding rate.

The fix is one read-time helper, `IncomeSplit`: an estimate's withholding is its gross
at the security's ladder rate and its net is the gross less exactly that, so
``net + withholding == gross`` by construction. Stored rows are unchanged.
"""

import ast
import pathlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.dividend_repository import DividendRepository
from app.services.activity_service import DIVIDEND, MAX_LIMIT, ActivityService
from app.services.dividend_service import DividendService

AS_OF = date(2026, 12, 31)


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="Alpha Corp",
                         currency="EUR", conid=100, asset_category="STK", exchange="NYSE"))
    session.add(Security(id=2, isin="DE0000000002", symbol="BBB", description="Beta AG",
                         currency="EUR", conid=200, asset_category="STK", exchange="XETRA"))
    await session.flush()
    return engine, session


async def _estimate(session, security_id, on, gross):
    await DividendRepository(session).upsert_payment({
        "security_id": security_id, "ex_date": on, "pay_date": None, "currency": "EUR",
        "amount_per_share": Decimal("1"), "shares_held": Decimal(gross),
        "gross_amount_eur": Decimal(gross), "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": Decimal(gross),     # what compute_dividend_income stores
        "source": "yfinance_estimate",
    })


async def _ibkr(session, security_id, on, gross, wht):
    await DividendRepository(session).upsert_payment({
        "security_id": security_id, "ex_date": on, "pay_date": on, "currency": "EUR",
        "shares_held": Decimal("0"), "gross_amount_eur": Decimal(gross),
        "withholding_tax_eur": Decimal(wht),
        "net_amount_eur": Decimal(gross) - Decimal(wht), "source": "ibkr",
    })


def _year(response, y):
    return next(r for r in response["growth"]["annual"] if r["year"] == y)


@pytest.mark.asyncio
async def test_an_estimate_era_year_has_net_plus_withholding_equal_to_gross():
    """
    The acceptance criterion. AAA has IBKR's measured 15%; BBB, never paid by IBKR, takes
    the WHT setting (20% here, so the two rungs are told apart). Gross 150 in 2025 is net
    125 and withholding 25 — before the fix it read net 150 beside withholding 25.
    """
    engine, session = await _make_session()
    try:
        await AppSettingsRepository(session).set_dividend_net_factor(Decimal("0.8"))
        await _ibkr(session, 1, date(2026, 6, 15), "100", "15")
        await _estimate(session, 1, date(2025, 3, 15), "60")
        await _estimate(session, 1, date(2025, 9, 15), "40")
        await _estimate(session, 2, date(2025, 5, 15), "50")
        svc = DividendService(session)

        for forecast in (False, True):
            r = await svc.get_dividend_breakdown(as_of=AS_OF, include_forecast=forecast)
            y2025 = _year(r, 2025)
            assert y2025["net_eur"] == 125.0, forecast
            assert y2025["withholding_eur"] == 25.0, forecast
            assert y2025["net_eur"] + y2025["withholding_eur"] == 150.0

        per_sec = {
            row["symbol"]: row for row in
            (await svc.get_dividend_breakdown(year=2025, as_of=AS_OF,
                                              include_forecast=False))["securities"]
        }
        assert per_sec["AAA"]["net_eur"] == 85.0 and per_sec["AAA"]["gross_eur"] == 100.0
        assert per_sec["BBB"]["net_eur"] == 40.0 and per_sec["BBB"]["gross_eur"] == 50.0
        # The recorded withholding stays what was recorded; the client draws it absent.
        assert per_sec["BBB"]["withholding_eur"] == 0.0
        assert per_sec["BBB"]["source"] == "estimate"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_growth_across_the_splice_compares_net_with_net():
    """
    The same dividend, gross 100, once estimated (2025) and once paid by IBKR with 15%
    withheld (2026). Flat income: 0.0% — it read -15.0% when the estimate counted gross.
    """
    engine, session = await _make_session()
    try:
        await _estimate(session, 1, date(2025, 6, 15), "100")
        await _ibkr(session, 1, date(2026, 6, 15), "100", "15")

        r = await DividendService(session).get_dividend_breakdown(
            as_of=AS_OF, include_forecast=False,
        )
        assert _year(r, 2025)["net_eur"] == 85.0
        assert _year(r, 2026)["net_eur"] == 85.0
        assert _year(r, 2026)["yoy_pct"] == 0.0
        assert r["growth"]["ttm"] == {"net_eur": 85.0, "prev_net_eur": 85.0, "pct": 0.0}
        assert r["growth"]["ttm_crosses_era"] is True
        assert r["growth"]["ytd"]["pct"] == 0.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_ibkr_era_reads_exactly_what_ibkr_paid():
    """IBKR rows are untouched: their own net and withholding, to the cent."""
    engine, session = await _make_session()
    try:
        await _estimate(session, 1, date(2025, 6, 15), "100")
        await _ibkr(session, 1, date(2026, 6, 15), "100", "15")
        await _ibkr(session, 2, date(2026, 7, 15), "50", "13.19")

        r = await DividendService(session).get_dividend_breakdown(
            year=2026, as_of=AS_OF, include_forecast=False,
        )
        y2026 = _year(r, 2026)
        assert y2026["net_eur"] == 121.81
        assert y2026["withholding_eur"] == 28.19
        assert r["total_net_eur"] == 121.81
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_summary_card_and_the_ledger_carry_the_same_net_as_the_tab():
    """Three readers of the spliced rows, one net per dividend."""
    engine, session = await _make_session()
    try:
        await _estimate(session, 1, date(2025, 6, 15), "100")
        await _estimate(session, 2, date(2025, 8, 15), "40")
        await _ibkr(session, 1, date(2026, 6, 15), "100", "15")
        svc = DividendService(session)

        breakdown = await svc.get_dividend_breakdown(as_of=AS_OF, include_forecast=False)
        tab_total = sum(Decimal(str(y["net_eur"])) for y in breakdown["growth"]["annual"])

        summary = await svc.get_dividend_summary()
        assert Decimal(str(summary["total_net_eur"])) == tab_total == Decimal("204")
        assert summary["total_gross_eur"] == summary["total_net_eur"] + summary["total_withholding_eur"]

        ledger = await ActivityService(session).get_activity(
            start_date=date(2025, 1, 1), end_date=AS_OF, kinds=[DIVIDEND], limit=MAX_LIMIT,
        )
        assert sum(Decimal(str(i["amount_base"])) for i in ledger["items"]) == tab_total
    finally:
        await session.close()
        await engine.dispose()


# ── The family rule ───────────────────────────────────────────────────────────

# Readers that splice dividend rows and deliberately keep the stored net. Each entry must
# say why, and must still splice — a stale entry fails below.
KEEPS_THE_STORED_NET = {
    "tax_service.py": (
        "a filing aid states no assumed withholding: an estimate's withholding is absent "
        "there, and a net of gross less the ladder would publish it as gross minus net "
        "(docs/tax.md)"
    ),
}


def _attributes(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_every_reader_of_spliced_income_reads_an_estimates_net_through_the_split():
    """
    "Is there a copy at all", not "do the copies agree": a module that splices the two
    sources and then reads a row's net itself (`_net_eur`) counts an estimate at gross,
    which is the bug. Scoped by what the rule is about — every module under `app/` that
    calls `_splice_by_era` — not by directory.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    splicers, offenders = [], []
    for path in sorted(root.rglob("*.py")):
        if path.name == "dividend_service.py":
            continue    # the definitions live here
        attrs = _attributes(path)
        if "_splice_by_era" not in attrs:
            continue
        splicers.append(path.name)
        if path.name in KEEPS_THE_STORED_NET:
            continue
        if "income_split" not in attrs or "_net_eur" in attrs:
            offenders.append(path.name)

    assert not offenders, (
        f"{offenders} splice dividend rows without reading an estimate's net through "
        "DividendService.income_split(). An estimate stores its gross as net; read "
        "directly it puts gross beside IBKR's net in a net figure (issue #61)."
    )
    stale = set(KEEPS_THE_STORED_NET) - set(splicers)
    assert not stale, f"{sorted(stale)} no longer splice; drop the exemption"
    # A floor, so a renamed helper cannot make the walk pass by finding nothing.
    assert {"activity_service.py", "portfolio_service.py"} <= set(splicers)
