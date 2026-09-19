"""
A projected dividend is dated when the CASH is expected, and stays visible until it
arrives.

Three rules used to combine into a hole. `project_dividends` infers a cadence from
yfinance's **ex-date** series, so every projected date was an ex-date; `horizon_start =
as_of + 1` deletes a projection on its own date; and `_splice_by_era` drops the estimate
recording that same payment once the IBKR era has begun. So from the ex-date until the
cash landed, the dividend was in no figure at all — `upcoming`, `months[]`,
`ttm_series`, `next_12m_eur`, `forward_yield`, the tax report, the ledger and XIRR
alike. Measured on production 2026-09-19: up to 29 days, on six held securities at once.

What closes it, weakest last:

- an **accrual** — IBKR's own announced pay date, the only record carrying both dates;
- a **measured lag** — this security's own ex→pay distance, paired out of the raw
  history by the matcher the era splice already runs;
- the **ex-date**, unchanged, and now saying that is what it is.

Everything that reaches the calendar reaches the forecast buckets with it, dated where
the calendar dates it: a payment that has gone ex is the most certain money on the list,
and it used to be the only kind absent from every chart and every total. What it never
reaches is the realized side, or `next_12m_eur`/`forward_yield` once its date has passed
— a backlog entry is before that window, not inside it.

Which means a projection CAN now sit inside an elapsed month, so `partial` keeps meaning
"this window has not fully elapsed" and the client hides a projection by stripping it
from the point rather than by dropping the point. `_forecast_inputs` and the forward loop
still never date one on or before today; that is now a property of the forward series
alone, pinned below.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.clock import utcnow
from app.database import Base
import app.models  # noqa: F401
from app.models.dividend_accrual import DividendAccrual
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.services.dividend_service import (
    DividendService,
    EX_TO_PAY_MAX_LAG_DAYS,
    PENDING_MAX_AGE_DAYS,
    match_estimates_to_ibkr,
)

AS_OF = date(2026, 5, 1)
LAG = 14


class Row:
    """The three attributes the matcher and the lag reader touch."""

    def __init__(self, security_id, source, ex_date, pay_date=None):
        self.security_id = security_id
        self.source = source
        self.ex_date = ex_date
        self.pay_date = pay_date


async def _session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="NL0010273215", symbol="ASML", description="ASML",
                         currency="EUR", conid=100, asset_category="STK", exchange="AEB"))
    session.add(TaxLot(
        security_id=1, open_date=date(2024, 1, 2), quantity=Decimal("10"),
        cost_basis=Decimal("6000"), cost_basis_eur=Decimal("6000"),
        price_per_unit=Decimal("600"), currency="EUR", is_open=True,
    ))
    session.add(MarketPrice(security_id=1, date=date.today(),
                            close_price=Decimal("800"), currency="EUR", source="test"))
    await session.flush()
    return engine, session


async def _seed_quarterly(session, *, lag_days=LAG, quarters=4, last_ex=date(2026, 2, 8)):
    """
    A quarterly payer recorded the way production records one: a yfinance estimate on
    each ex-date, and an IBKR row ``lag_days`` later carrying the real net.

    Both rows for one payment is not duplication — it is exactly the shape the ex→pay
    distance is measured out of, and the shape `_splice_by_era` exists to reconcile.

    The default ends on 2026-02-08, far enough behind ``AS_OF`` that a test can place a
    gone-ex-but-unpaid estimate in the spring without an already-seeded IBKR payment
    accidentally covering it.
    """
    repo = DividendRepository(session)
    exes = [last_ex - timedelta(days=91 * i) for i in range(quarters)][::-1]
    for ex in exes:
        await repo.upsert_payment({
            "security_id": 1, "ex_date": ex, "currency": "EUR",
            "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
            "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
        })
        pay = ex + timedelta(days=lag_days)
        await repo.upsert_payment({
            "security_id": 1, "ex_date": pay, "pay_date": pay, "currency": "EUR",
            "shares_held": Decimal("0"), "gross_amount_eur": Decimal("15"),
            "withholding_tax_eur": Decimal("2"), "net_amount_eur": Decimal("13"),
            "source": "ibkr",
        })
    await session.commit()
    return exes


async def _breakdown(session, **kw):
    kw.setdefault("include_forecast", True)
    kw.setdefault("as_of", AS_OF)
    return await DividendService(session).get_dividend_breakdown(**kw)


# --- the matcher, now shared ------------------------------------------------------

def test_the_matcher_returns_the_pairs_the_splice_used_to_compute_inline():
    ibkr = [Row(1, "ibkr", date(2026, 2, 18), date(2026, 2, 18))]
    near = Row(1, "yfinance_estimate", date(2026, 2, 9))
    far = Row(1, "yfinance_estimate", date(2026, 1, 2))
    pairs = match_estimates_to_ibkr([near, far], ibkr)
    assert [(e.ex_date, r.pay_date) for e, r in pairs] == [
        (date(2026, 2, 9), date(2026, 2, 18))
    ]


def test_a_wider_window_is_a_parameter_because_the_two_callers_want_different_ones():
    """The splice must err narrow — too wide deletes real income. A lag measured a few
    days out only mis-dates a projection, so the ceiling is the caller's to choose."""
    ibkr = [Row(1, "ibkr", date(2026, 3, 20), date(2026, 3, 20))]
    est = [Row(1, "yfinance_estimate", date(2026, 2, 5))]   # 43 days — Asian-payer shaped
    assert match_estimates_to_ibkr(est, ibkr) == []
    assert len(match_estimates_to_ibkr(est, ibkr, max_lag_days=60)) == 1


# --- the measured lag -------------------------------------------------------------

def test_the_lag_is_the_median_of_the_observed_distances():
    ibkr, est = [], []
    for ex, pay in ((date(2026, 1, 5), date(2026, 1, 16)),     # 11
                    (date(2026, 4, 6), date(2026, 4, 27))):    # 21
        est.append(Row(1, "yfinance_estimate", ex))
        ibkr.append(Row(1, "ibkr", pay, pay))
    lags = DividendService._measured_pay_lags(est + ibkr)
    assert lags == {1: (16, 2)}


def test_a_security_never_paid_through_ibkr_has_no_lag_rather_than_a_zero_one():
    """Absence says 'this date is an ex-date'. A 0 would claim same-day settlement."""
    est = [Row(1, "yfinance_estimate", date(2026, 1, 5)),
           Row(1, "yfinance_estimate", date(2026, 4, 6))]
    assert DividendService._measured_pay_lags(est) == {}


def test_the_lag_is_measured_per_security_and_never_pooled():
    rows = [
        Row(1, "yfinance_estimate", date(2026, 1, 5)),
        Row(1, "ibkr", date(2026, 1, 12), date(2026, 1, 12)),     # 7
        Row(2, "yfinance_estimate", date(2026, 1, 5)),
        Row(2, "ibkr", date(2026, 2, 2), date(2026, 2, 2)),       # 28
    ]
    assert DividendService._measured_pay_lags(rows) == {1: (7, 1), 2: (28, 1)}


# --- the shift --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_projection_is_dated_when_the_cash_is_expected_not_on_the_ex_date():
    engine, session = await _session()
    await _seed_quarterly(session)
    data = await _breakdown(session)
    row = next(r for r in data["securities"] if r["security_id"] == 1)
    assert row["forecast_lag_days"] == LAG
    assert row["forecast_lag_samples"] == 4
    # A ~91-day gap snaps to the calendar quarter, so the next ex-date keeps the
    # schedule's own day of the month: 2026-02-08 + 3 months. The cash follows a lag on.
    assert data["upcoming"][0]["ex_date"] == "2026-05-08"
    assert data["upcoming"][0]["date"] == "2026-05-22"
    assert data["upcoming"][0]["pay_date_source"] == "measured_lag"
    await engine.dispose()


@pytest.mark.asyncio
async def test_without_a_measurable_lag_the_date_is_the_ex_date_and_says_so():
    engine, session = await _session()
    repo = DividendRepository(session)
    for ex in (date(2025, 10, 8), date(2026, 1, 8), date(2026, 4, 8)):
        await repo.upsert_payment({
            "security_id": 1, "ex_date": ex, "currency": "EUR",
            "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
            "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
        })
    await session.commit()
    data = await _breakdown(session)
    assert data["upcoming"][0]["pay_date_source"] == "ex_date"
    assert data["upcoming"][0]["date"] == data["upcoming"][0]["ex_date"]
    row = next(r for r in data["securities"] if r["security_id"] == 1)
    assert row["forecast_lag_days"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_ibkr_only_cadence_is_already_pay_dated_and_is_not_shifted_twice():
    """`_forecast_inputs` only calls a schedule ex-dated when yfinance defined it. A
    security whose history is IBKR rows alone is dated on pay dates already."""
    engine, session = await _session()
    repo = DividendRepository(session)
    for pay in (date(2025, 10, 22), date(2026, 1, 22), date(2026, 4, 22)):
        await repo.upsert_payment({
            "security_id": 1, "ex_date": pay, "pay_date": pay, "currency": "EUR",
            "shares_held": Decimal("0"), "gross_amount_eur": Decimal("15"),
            "withholding_tax_eur": Decimal("2"), "net_amount_eur": Decimal("13"),
            "source": "ibkr",
        })
    await session.commit()
    data = await _breakdown(session)
    assert data["upcoming"], "an IBKR-only history still projects"
    assert data["upcoming"][0]["pay_date_source"] == "ex_date"
    assert data["upcoming"][0]["date"] == date(2026, 7, 22).isoformat()
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_payment_whose_ex_date_has_just_passed_is_projected_not_lost():
    """
    The projection is asked for a lag EARLIER than the horizon, so a payment going ex in
    the last few days still appears — dated when its cash lands.

    Last ex 2026-01-26 puts the next one on 2026-04-26, five days before `as_of`: under
    the old start it was never produced, and the estimate recording it had not synced
    yet, so the payment existed nowhere.
    """
    engine, session = await _session()
    await _seed_quarterly(session, last_ex=AS_OF - timedelta(days=95))
    data = await _breakdown(session)
    assert data["upcoming"][0]["ex_date"] == "2026-04-26"
    assert data["upcoming"][0]["date"] == "2026-05-10"
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_projection_is_ever_dated_on_or_before_today():
    """The invariant the Forecast toggle rests on: an elapsed month provably contains no
    projection, so dropping open windows reproduces the closed series exactly. Widening
    the projection's own start must not leak a date back across `as_of`."""
    engine, session = await _session()
    await _seed_quarterly(session, last_ex=AS_OF - timedelta(days=95))
    data = await _breakdown(session)
    assert data["upcoming"], "the fixture must actually project something"
    for u in data["upcoming"]:
        if u["pending"]:
            continue
        assert date.fromisoformat(u["date"]) > AS_OF
    for m in data["months"]:
        if m["month"] < AS_OF.strftime("%Y-%m"):
            assert m["forecast_total_eur"] == 0
    await engine.dispose()


# --- the pending tail -------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_dividend_that_has_gone_ex_stays_visible_until_the_cash_arrives():
    engine, session = await _session()
    await _seed_quarterly(session)
    # A further ex-date, five days ago: yfinance has it, IBKR has not paid it yet.
    recent_ex = AS_OF - timedelta(days=5)
    await DividendRepository(session).upsert_payment({
        "security_id": 1, "ex_date": recent_ex, "currency": "EUR",
        "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
        "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
    })
    await session.commit()

    data = await _breakdown(session)
    entry = next(u for u in data["upcoming"] if u["ex_date"] == recent_ex.isoformat())
    assert entry["date"] == (recent_ex + timedelta(days=LAG)).isoformat()
    assert entry["pending"] is False      # the cash is not due yet
    assert entry["basis"] == "gross_estimate"

    # Declared, gone ex, cash due inside the next twelve months: the most certain
    # money on the calendar, and until this change the only kind excluded from the
    # forward figures. Excluding it did not merely omit a payment — the cadence
    # steps PAST a recorded ex-date, so `next_12m_eur` lost one payment per security
    # from the moment yfinance wrote the row until the cash landed.
    bar = next(m for m in data["months"] if m["month"] == entry["date"][:7])
    assert bar["forecast_total_eur"] >= entry["net_eur"]
    assert data["growth"]["next_12m_eur"] == pytest.approx(
        sum(u["net_eur"] for u in data["upcoming"] if not u["pending"]), abs=0.02
    )
    row = next(r for r in data["securities"] if r["security_id"] == 1)
    assert row["next_pay_date"] == entry["date"]
    assert row["forward_yield_pct"] is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_overdue_payment_is_marked_pending_rather_than_dropped():
    engine, session = await _session()
    await _seed_quarterly(session)
    overdue_ex = AS_OF - timedelta(days=LAG + 10)
    await DividendRepository(session).upsert_payment({
        "security_id": 1, "ex_date": overdue_ex, "currency": "EUR",
        "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
        "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
    })
    await session.commit()
    data = await _breakdown(session)
    entry = next(u for u in data["upcoming"] if u["ex_date"] == overdue_ex.isoformat())
    assert entry["pending"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_pending_entry_disappears_once_the_ibkr_row_lands():
    engine, session = await _session()
    await _seed_quarterly(session)
    recent_ex = AS_OF - timedelta(days=20)
    repo = DividendRepository(session)
    await repo.upsert_payment({
        "security_id": 1, "ex_date": recent_ex, "currency": "EUR",
        "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
        "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
    })
    await session.commit()
    before = await _breakdown(session)
    assert any(u["ex_date"] == recent_ex.isoformat() for u in before["upcoming"])

    pay = recent_ex + timedelta(days=LAG)
    await repo.upsert_payment({
        "security_id": 1, "ex_date": pay, "pay_date": pay, "currency": "EUR",
        "shares_held": Decimal("0"), "gross_amount_eur": Decimal("15"),
        "withholding_tax_eur": Decimal("2"), "net_amount_eur": Decimal("13"),
        "source": "ibkr",
    })
    await session.commit()
    after = await _breakdown(session)
    assert not any(u["ex_date"] == recent_ex.isoformat() for u in after["upcoming"])
    # ...and it is realized income now, not a promise. In the same bar: the
    # translucent segment gives way to a solid one, which is the whole point of
    # showing the expected payment there rather than only on the calendar.
    entry = next(u for u in before["upcoming"] if u["ex_date"] == recent_ex.isoformat())
    mk = pay.strftime("%Y-%m")
    was = next(m for m in before["months"] if m["month"] == mk)
    now = next(m for m in after["months"] if m["month"] == mk)
    assert now["forecast_total_eur"] == pytest.approx(
        was["forecast_total_eur"] - entry["net_eur"]
    )
    assert now["actual_total_eur"] == pytest.approx(was["actual_total_eur"] + 13)
    assert after["total_net_eur"] > before["total_net_eur"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_payment_that_never_arrives_leaves_the_calendar_rather_than_sitting_there():
    """
    Bounded, like every other re-ask here. A payment IBKR reclassifies or books under
    another instrument never lands, and a permanent entry is how a reader learns to stop
    reading the calendar.

    Both rows are checked at once, because "absent" only means the bound when something
    the same shape and inside it is present.
    """
    engine, session = await _session()
    await _seed_quarterly(session)
    stale = date(2026, 1, 15)               # 106 days — past the bound
    recent = date(2026, 4, 7)               # 24 days — inside it
    assert (AS_OF - stale).days > PENDING_MAX_AGE_DAYS >= (AS_OF - recent).days
    repo = DividendRepository(session)
    for ex in (stale, recent):
        await repo.upsert_payment({
            "security_id": 1, "ex_date": ex, "currency": "EUR",
            "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
            "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
        })
    await session.commit()
    data = await _breakdown(session)
    seen = {u["ex_date"] for u in data["upcoming"]}
    assert recent.isoformat() in seen
    assert stale.isoformat() not in seen
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_pending_payment_is_projected_income_and_never_realized_income():
    """
    The cash has not arrived, so crediting it as income would pay the account money it
    has not received — but it is money the portfolio expects, which is what the forecast
    buckets hold. It lands in the bar for the month it was due, translucent, and in none
    of the realized figures.

    The *forecast* totals move by more than this entry, because the same new row is also
    a new last-known payment and the schedule steps from it; asserting those equal would
    be asserting the forecast ignores its own history. So the claim is made on the entry's
    own month, which the plain fixture leaves empty.
    """
    engine, session = await _session()
    await _seed_quarterly(session)
    plain = await _breakdown(session)

    overdue_ex = AS_OF - timedelta(days=LAG + 5)
    await DividendRepository(session).upsert_payment({
        "security_id": 1, "ex_date": overdue_ex, "currency": "EUR",
        "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
        "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
    })
    await session.commit()
    withp = await _breakdown(session)

    entry = next(u for u in withp["upcoming"] if u["ex_date"] == overdue_ex.isoformat())
    assert entry["pending"] is True
    was = next(m for m in plain["months"] if m["month"] == entry["date"][:7])
    bar = next(m for m in withp["months"] if m["month"] == entry["date"][:7])
    assert (was["actual_total_eur"], was["forecast_total_eur"]) == (0, 0)
    assert bar["actual_total_eur"] == 0
    assert bar["forecast_total_eur"] == entry["net_eur"] > 0
    assert withp["total_net_eur"] == plain["total_net_eur"]
    assert withp["growth"]["ttm"] == plain["growth"]["ttm"]
    assert withp["growth"]["ytd"] == plain["growth"]["ytd"]
    await engine.dispose()


# --- accruals ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_announced_pay_date_outranks_the_inferred_one():
    engine, session = await _session()
    await _seed_quarterly(session)
    # Inference would say 2026-05-08 ex, so 2026-05-22 paid. IBKR says the 18th.
    announced = date(2026, 5, 18)
    session.add(DividendAccrual(
        security_id=1, ex_date=date(2026, 5, 8), pay_date=announced,
        currency="EUR", net_amount_eur=Decimal("12.5"), last_seen_at=utcnow(),
    ))
    await session.commit()

    data = await _breakdown(session)
    first = data["upcoming"][0]
    assert first["pay_date_source"] == "accrual"
    assert first["date"] == announced.isoformat()
    assert first["basis"] == "net"
    # The inference it supersedes is gone, not sitting beside it.
    assert sum(1 for u in data["upcoming"]
               if abs((date.fromisoformat(u["date"]) - announced).days) < 30) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_accrual_never_becomes_income():
    """The reason accruals live in their own table. `dividend_payments` is read through
    `_splice_by_era` by the tax report, XIRR and the cash balance."""
    engine, session = await _session()
    await _seed_quarterly(session)
    plain = await _breakdown(session)
    session.add(DividendAccrual(
        security_id=1, ex_date=AS_OF + timedelta(days=10),
        pay_date=AS_OF + timedelta(days=24), currency="EUR",
        net_amount_eur=Decimal("12.5"), last_seen_at=utcnow(),
    ))
    await session.commit()
    withacc = await _breakdown(session)
    assert withacc["total_net_eur"] == plain["total_net_eur"]
    assert withacc["growth"]["ttm"] == plain["growth"]["ttm"]
    receipts = await DividendService(session).ibkr_cash_receipts()
    assert all(net == Decimal("13") for _, net in receipts)
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_accrual_ibkr_has_not_paid_is_pending_rather_than_forgotten():
    engine, session = await _session()
    await _seed_quarterly(session)
    session.add(DividendAccrual(
        security_id=1, ex_date=AS_OF - timedelta(days=20),
        pay_date=AS_OF - timedelta(days=3), currency="EUR",
        net_amount_eur=Decimal("12.5"), last_seen_at=utcnow(),
    ))
    await session.commit()
    data = await _breakdown(session)
    entry = next(u for u in data["upcoming"] if u["pay_date_source"] == "accrual")
    assert entry["pending"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_accrual_set_is_replaced_wholesale_so_a_paid_one_cannot_linger():
    from app.repositories.dividend_accrual_repository import DividendAccrualRepository

    engine, session = await _session()
    repo = DividendAccrualRepository(session)
    await repo.replace_all([{
        "security_id": 1, "ex_date": date(2026, 5, 10), "pay_date": date(2026, 5, 24),
        "currency": "EUR", "net_amount_eur": Decimal("12.5"),
        "last_seen_at": utcnow(),
    }])
    assert len(await repo.get_open()) == 1
    # The next statement no longer lists it, which is IBKR saying it was paid.
    assert await repo.replace_all([]) == 0
    assert await repo.get_open() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_absent_flex_section_is_a_supported_state():
    """The section is off by default in the portal, and the ingest runs unconditionally
    so that ticking it is the whole setup."""
    from app.services.ibkr_service import IBKRService

    class _Statement:
        pass

    engine, session = await _session()
    assert await IBKRService().extract_dividend_accruals({"statement": _Statement()}) == []
    result = await DividendService(session).sync_dividend_accruals([], {})
    assert result["dividend_accruals"] == 0
    data = await _breakdown(session)
    assert data["upcoming"] == []
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("factor, expected", [(None, 85), (Decimal("0.72"), 72)])
async def test_one_factor_sizes_both_gross_fallbacks_without_mutating_history(monkeypatch, factor, expected):
    import app.services.dividend_service as module
    from sqlalchemy import select
    from app.models.dividend_payment import DividendPayment

    if factor is not None:
        monkeypatch.setattr(module, "DEFAULT_DIVIDEND_NET_FACTOR", factor)
    engine, session = await _session()
    try:
        repo = DividendRepository(session)
        # Establish the IBKR era without a matching payment for the recent ex-date.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2025, 1, 10), "currency": "EUR",
            "gross_amount_eur": Decimal("100"), "net_amount_eur": Decimal("91"),
            "withholding_tax_eur": Decimal("9"), "source": "ibkr",
        })
        for ex in (date(2025, 7, 8), date(2025, 10, 8), date(2026, 1, 8), date(2026, 4, 8)):
            await repo.upsert_payment({
                "security_id": 1, "ex_date": ex, "currency": "EUR",
                "shares_held": Decimal("10"), "amount_per_share": Decimal("10"),
                "gross_amount_eur": Decimal("100"), "net_amount_eur": Decimal("100"),
                "withholding_tax_eur": Decimal("0"), "source": "yfinance_estimate",
            })
        await session.commit()
        first = await _breakdown(session)
        assert first["upcoming"]
        assert {u["net_eur"] for u in first["upcoming"]} == {expected}
        assert any(u["pending"] for u in first["upcoming"])
        assert any(not u["pending"] for u in first["upcoming"])
        assert first["total_forecast_net_eur"] > 0
        assert first["total_net_eur"] == 91
        assert await _breakdown(session) == first
        session.expire_all()
        stored = (await session.execute(select(DividendPayment).where(
            DividendPayment.source == "yfinance_estimate",
        ))).scalars().all()
        assert len(stored) == 4
        assert all(p.gross_amount_eur == p.net_amount_eur == Decimal("100") for p in stored)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_net, tax, expected", [
    (Decimal("92"), Decimal("-15"), 92),
    (None, Decimal("-8"), 92),
])
async def test_pending_estimate_hands_off_to_accrual_then_actual_cash(reported_net, tax, expected):
    engine, session = await _session()
    try:
        await _seed_quarterly(session)
        repo = DividendRepository(session)
        ex = date(2026, 4, 8)
        pay = ex + timedelta(days=LAG)
        await repo.upsert_payment({
            "security_id": 1, "ex_date": ex, "currency": "EUR",
            "shares_held": Decimal("10"), "amount_per_share": Decimal("10"),
            "gross_amount_eur": Decimal("100"), "net_amount_eur": Decimal("100"),
            "withholding_tax_eur": Decimal("0"), "source": "yfinance_estimate",
        })
        await session.commit()
        svc = DividendService(session)
        before = await _breakdown(session)
        entry = next(u for u in before["upcoming"] if u["ex_date"] == ex.isoformat())
        assert (entry["net_eur"], entry["pending"]) == (85, True)

        await svc.sync_dividend_accruals([{
            "conid": 100, "ex_date": ex, "pay_date": pay, "currency": "EUR",
            "gross_amount": Decimal("100"), "tax": tax, "net_amount": reported_net,
        }], {"100": 1})
        await session.commit()
        accrued = await _breakdown(session)
        entries = [u for u in accrued["upcoming"] if u["ex_date"] == ex.isoformat()]
        assert len(entries) == 1
        assert (entries[0]["net_eur"], entries[0]["basis"], entries[0]["pending"]) == (expected, "net", True)
        assert entries[0]["pay_date_source"] == "accrual"
        assert accrued["total_net_eur"] == before["total_net_eur"]
        # The calendar entry is replaced one-for-one, so the bar it sits in moves by
        # exactly the difference between the two amounts and by nothing else — the
        # accrual is IBKR's own figure superseding our estimated net, which is the
        # whole reason it outranks it. Realized income does not move at all.
        moved = entries[0]["net_eur"] - entry["net_eur"]
        for was, now in zip(before["months"], accrued["months"], strict=True):
            assert now["month"] == was["month"]
            assert now["actual"] == was["actual"]
            delta = pytest.approx(moved if now["month"] == pay.strftime("%Y-%m") else 0)
            assert now["forecast_total_eur"] - was["forecast_total_eur"] == delta
        for was, now in zip(before["ttm_series"], accrued["ttm_series"], strict=True):
            assert now["month"] == was["month"] and now["net_eur"] == was["net_eur"]

        await repo.upsert_payment({
            "security_id": 1, "ex_date": pay, "pay_date": pay, "currency": "EUR",
            "gross_amount_eur": Decimal("100"), "net_amount_eur": Decimal("91"),
            "withholding_tax_eur": Decimal("9"), "source": "ibkr",
        })
        # Normal snapshot replacement clears the accrual after IBKR pays it.
        await svc.sync_dividend_accruals([], {"100": 1})
        await session.commit()
        paid = await _breakdown(session)
        assert not any(u["ex_date"] == ex.isoformat() for u in paid["upcoming"])
        assert paid["total_net_eur"] == before["total_net_eur"] + 91
        assert (pay, Decimal("91")) in await svc.ibkr_cash_receipts()
    finally:
        await session.close()
        await engine.dispose()
# --- the projection tail: when NOTHING records the payment yet ---------------------

async def _seed_estimates_only(session, *, last_ex, gap_days=91, count=4):
    """
    A payer yfinance records and IBKR has never paid — VT's exact shape on production:
    a dozen estimate rows, no cash transaction of its own, so no lag is measurable for
    it and its projected dates stay ex-dates.
    """
    repo = DividendRepository(session)
    exes = [last_ex - timedelta(days=gap_days * i) for i in range(count)][::-1]
    for ex in exes:
        await repo.upsert_payment({
            "security_id": 1, "ex_date": ex, "currency": "EUR",
            "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
            "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
        })
    await session.commit()
    return exes


async def _seed_ibkr_era(session):
    """
    One IBKR payment on an UNRELATED security, long before anything else happens.

    The era boundary is the account's, not the security's: on production `ibkr_from` is
    2026-02-18 while VT has never paid a cent through IBKR. Without this the estimate
    tail is switched off entirely (`ibkr_from is None`) and the handoff below would be
    measured against a state the account has not been in since February.
    """
    session.add(Security(id=2, isin="US0378331005", symbol="AAPL", description="Apple",
                         currency="USD", conid=200, asset_category="STK",
                         exchange="NASDAQ"))
    await session.flush()
    await DividendRepository(session).upsert_payment({
        "security_id": 2, "ex_date": date(2025, 6, 15), "pay_date": date(2025, 6, 15),
        "currency": "EUR", "shares_held": Decimal("0"),
        "gross_amount_eur": Decimal("5"), "withholding_tax_eur": Decimal("1"),
        "net_amount_eur": Decimal("4"), "source": "ibkr",
    })
    await session.commit()


# Last ex-date 2026-01-30, quarterly: the cadence puts the next payment on 2026-04-30,
# which is YESTERDAY. That is the VT case exactly — ex 2026-09-18, read on the 19th.
VT_LAST_EX = date(2026, 1, 30)
VT_DUE = date(2026, 4, 30)


@pytest.mark.asyncio
async def test_a_payment_the_cadence_predicted_survives_its_own_date():
    """
    The VT case. Nothing records this dividend: yfinance writes its series the day AFTER
    the ex-date, IBKR has never paid this security, and no accrual has been announced.
    All we have is our own cadence — which named the date correctly and then threw the
    payment away, because `horizon_start` is `as_of + 1`.
    """
    engine, session = await _session()
    await _seed_ibkr_era(session)
    await _seed_estimates_only(session, last_ex=VT_LAST_EX)

    data = await _breakdown(session)
    entry = next(u for u in data["upcoming"] if u["security_id"] == 1)
    assert entry["date"] == VT_DUE.isoformat() == (AS_OF - timedelta(days=1)).isoformat()
    assert entry["pending"] is True
    assert entry["pay_date_source"] == "ex_date"
    assert entry["ex_date"] == VT_DUE.isoformat()   # no lag to shift it by
    assert entry["basis"] == "gross_estimate"
    await engine.dispose()


@pytest.mark.asyncio
async def test_every_chart_total_is_the_calendar_summed_the_same_way():
    """
    The family question, not the instance one: which other code publishes the same money?
    Four producers feed `upcoming` — the cadence, an accrual, an estimate row and this
    inferred tail — and the chart totals must be that list re-summed, or a reader adding
    up the calendar lands somewhere the chart does not.

    Two sums, because the two figures answer different questions.
    `total_forecast_net_eur` is the selected window, so it takes everything dated in it.
    `next_12m_eur` is as_of -> as_of+365, so it takes what is still AHEAD — and `pending`
    is exactly the entries whose date has gone by, which makes it the slice.
    """
    engine, session = await _session()
    await _seed_ibkr_era(session)
    await _seed_estimates_only(session, last_ex=VT_LAST_EX)
    data = await _breakdown(session)

    entry = next(u for u in data["upcoming"] if u["security_id"] == 1)
    assert entry["pending"] is True and entry["net_eur"] > 0
    bar = next(m for m in data["months"] if m["month"] == entry["date"][:7])
    assert bar["actual_total_eur"] == 0
    assert bar["forecast_total_eur"] >= entry["net_eur"]

    forward = sum(u["net_eur"] for u in data["upcoming"] if not u["pending"])
    assert data["growth"]["next_12m_eur"] == pytest.approx(forward, abs=0.02)
    assert data["total_forecast_net_eur"] == pytest.approx(
        sum(u["net_eur"] for u in data["upcoming"]
            if u["date"][:4] == str(AS_OF.year)), abs=0.02
    )
    # ...and per month, so the right bar carries it rather than merely the right total.
    by_month: dict = {}
    for u in data["upcoming"]:
        if u["date"][:4] == str(AS_OF.year):
            by_month[u["date"][:7]] = by_month.get(u["date"][:7], 0) + u["net_eur"]
    for m in data["months"]:
        assert m["forecast_total_eur"] == pytest.approx(
            by_month.get(m["month"], 0), abs=0.02
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_next_pay_date_is_the_next_payment_not_the_overdue_one():
    """`next_pay_date` answers "when next", and an overdue payment is not next."""
    engine, session = await _session()
    await _seed_ibkr_era(session)
    await _seed_estimates_only(session, last_ex=VT_LAST_EX)
    data = await _breakdown(session)
    row = next(r for r in data["securities"] if r["security_id"] == 1)
    assert row["next_pay_date"] == "2026-07-30"
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_position_opened_after_the_ex_date_is_owed_nothing():
    """
    Entitlement is fixed on the ex-date. Buying the day after it buys no dividend, and
    an inference that ignored this would invent one for every position opened in the
    last three months.
    """
    engine, session = await _session()
    await _seed_ibkr_era(session)
    await _seed_estimates_only(session, last_ex=VT_LAST_EX)
    # Move the only lot to the day after the payment went ex.
    lot = (await session.execute(select(TaxLot))).scalars().first()
    lot.open_date = VT_DUE + timedelta(days=1)
    await session.commit()

    data = await _breakdown(session)
    assert [u for u in data["upcoming"] if u["pending"]] == []
    assert any(u["security_id"] == 1 for u in data["upcoming"]), \
        "the future projections must still be there"
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_overdue_payment_is_sized_by_the_holding_it_went_ex_with():
    """Shares bought after the ex-date carry no entitlement, so they must not size it."""
    engine, session = await _session()
    await _seed_ibkr_era(session)
    await _seed_estimates_only(session, last_ex=VT_LAST_EX)
    before = await _breakdown(session)
    owed = next(u for u in before["upcoming"] if u["pending"])["net_eur"]

    session.add(TaxLot(
        security_id=1, open_date=VT_DUE + timedelta(days=1), quantity=Decimal("90"),
        cost_basis=Decimal("54000"), cost_basis_eur=Decimal("54000"),
        price_per_unit=Decimal("600"), currency="EUR", is_open=True,
    ))
    await session.commit()

    after = await _breakdown(session)
    entry = next(u for u in after["upcoming"] if u["pending"])
    assert entry["net_eur"] == owed          # unchanged by the ten-fold position
    assert next(u for u in after["upcoming"] if not u["pending"])["net_eur"] > owed
    await engine.dispose()


# A cadence that does NOT snap to a calendar period, so the projected dates step in
# exact days and the expiry boundary can be landed on. 120 keeps the payer inside
# STOPPED_AFTER_GAPS at ninety days stale, which a monthly one would not.
SLOW_GAP = 120


@pytest.mark.asyncio
@pytest.mark.parametrize("age, still_shown", [
    (PENDING_MAX_AGE_DAYS, True),
    (PENDING_MAX_AGE_DAYS + 1, False),
])
async def test_an_inference_nothing_ever_confirms_stops_claiming(age, still_shown):
    """
    A synthetic payment expires; it is our own guess, and a guess nothing confirms in
    three months is a calendar entry the reader learns to ignore — the same reason the
    stale-basket banner had to become a refresh.

    Pinned against the constant rather than the arithmetic: the bound currently falls
    out of the projection's start date, and an edit there could unbound it in silence.
    """
    engine, session = await _session()
    await _seed_ibkr_era(session)
    await _seed_estimates_only(
        session, last_ex=AS_OF - timedelta(days=age + SLOW_GAP), gap_days=SLOW_GAP,
    )
    data = await _breakdown(session)
    due = (AS_OF - timedelta(days=age)).isoformat()
    assert [u["date"] for u in data["upcoming"] if u["pending"]] == (
        [due] if still_shown else []
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_accrual_outlives_the_bound_an_inference_is_held_to():
    """
    The asymmetry is the point. An accrual is IBKR asserting the money is still owed,
    and it leaves when IBKR stops saying so — the wholesale replace does that within a
    sync of the cash posting. Ageing it out would delete a live, stated liability.
    """
    engine, session = await _session()
    await _seed_quarterly(session)
    old = AS_OF - timedelta(days=PENDING_MAX_AGE_DAYS * 2)
    session.add(DividendAccrual(
        security_id=1, ex_date=old - timedelta(days=14), pay_date=old, currency="EUR",
        net_amount_eur=Decimal("12.5"), last_seen_at=utcnow(),
    ))
    await session.commit()
    data = await _breakdown(session)
    entry = next(u for u in data["upcoming"] if u["pay_date_source"] == "accrual")
    assert (entry["date"], entry["pending"]) == (old.isoformat(), True)
    await engine.dispose()


# --- the handoff --------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("estimate, accrual, cash, shown", [
    (False, False, False, 1),   # only our cadence knows
    (True,  False, False, 1),   # yfinance publishes it the next day
    (False, True,  False, 1),   # IBKR announces it
    (True,  True,  False, 1),   # both — the announcement wins, once
    (False, False, True,  0),   # the cash lands: realized income, off the calendar
    (True,  False, True,  0),
    (False, True,  True,  1),   # IBKR still calls the accrual open; its own replace ends it
    (True,  True,  True,  1),
])
async def test_one_dividend_is_on_the_calendar_at_most_once(estimate, accrual, cash, shown):
    """
    The walk is `projection → yfinance row / accrual → actual cash`, and every state
    along it must show the payment exactly once or not at all.

    Written as the whole table rather than one test per edge, because the question is
    "can this dividend ever appear twice" — a family question. Six separate assertions
    is how the seventh combination goes unwritten.
    """
    engine, session = await _session()
    await _seed_ibkr_era(session)
    await _seed_estimates_only(session, last_ex=VT_LAST_EX)
    repo = DividendRepository(session)

    if estimate:
        await repo.upsert_payment({
            "security_id": 1, "ex_date": VT_DUE, "currency": "EUR",
            "shares_held": Decimal("10"), "amount_per_share": Decimal("1.5"),
            "gross_amount_eur": Decimal("15"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("15"), "source": "yfinance_estimate",
        })
    if accrual:
        session.add(DividendAccrual(
            security_id=1, ex_date=VT_DUE, pay_date=VT_DUE + timedelta(days=3),
            currency="EUR", net_amount_eur=Decimal("12.75"), last_seen_at=utcnow(),
        ))
    if cash:
        paid = VT_DUE + timedelta(days=3)
        await repo.upsert_payment({
            "security_id": 1, "ex_date": paid, "pay_date": paid, "currency": "EUR",
            "shares_held": Decimal("0"), "gross_amount_eur": Decimal("15"),
            "withholding_tax_eur": Decimal("2"), "net_amount_eur": Decimal("13"),
            "source": "ibkr",
        })
    await session.commit()

    data = await _breakdown(session)
    same = [u for u in data["upcoming"]
            if u["security_id"] == 1
            and abs((date.fromisoformat(u["date"]) - VT_DUE).days) <= 7]
    assert len(same) == shown, same
    await engine.dispose()
