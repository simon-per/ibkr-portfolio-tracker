"""
The era splice leaked exactly one dividend per security, at the boundary itself.

`_splice_by_era` keeps yfinance estimates strictly before the first IBKR payment and
IBKR rows from there on. The two sources file the **same** payment under different
dates, though — yfinance under its ex-date, IBKR under its pay-date, weeks apart — so
the first IBKR payment's own estimate sits *before* the boundary and the rule keeps it,
beside the IBKR row it duplicates.

Found by reading the Activity ledger on production, boundary 2026-02-18, ASML held on
two exchanges:

    2026-02-09  yfinance_estimate      2026-02-18  ibkr
    2026-02-10  yfinance_estimate      2026-02-18  ibkr

Four rows for two dividends. Every reader that splices was affected — the breakdown, the
summary card, XIRR's dividend inflows, the tax report's DA-1 income and the ledger — so
it overstated dividend income for the boundary period on all of them at once.

The match is per security, nearest-first, one-to-one and bounded by
`EX_TO_PAY_MAX_LAG_DAYS`. Never by amount: one side is gross and the other net, so equal
amounts are exactly what you cannot rely on.
"""

import pathlib
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace


from app.services.dividend_service import DividendService, EX_TO_PAY_MAX_LAG_DAYS

BOUNDARY = date(2026, 2, 18)


def _p(security_id, on_date, source, *, pay=None):
    """A payment stub: yfinance rows carry an ex-date, IBKR rows a pay-date."""
    if source == "ibkr":
        return SimpleNamespace(
            security_id=security_id, source="ibkr",
            ex_date=None, pay_date=pay or on_date, net_amount_eur=Decimal("1"),
        )
    return SimpleNamespace(
        security_id=security_id, source="yfinance_estimate",
        ex_date=on_date, pay_date=None, net_amount_eur=Decimal("1"),
    )


def _dates(kept):
    return sorted((p.ex_date or p.pay_date, p.source) for p in kept)


def test_the_boundary_dividend_is_not_counted_from_both_sources():
    """The production shape: ASML dual-listed, estimate 8-9 days before its pay date."""
    payments = [
        _p(1, date(2026, 2, 9), "yfinance_estimate"),
        _p(2, date(2026, 2, 10), "yfinance_estimate"),
        _p(1, BOUNDARY, "ibkr"),
        _p(2, BOUNDARY, "ibkr"),
    ]
    kept, boundary = DividendService._splice_by_era(payments)

    assert boundary == BOUNDARY
    assert _dates(kept) == [(BOUNDARY, "ibkr"), (BOUNDARY, "ibkr")]


def test_a_genuinely_earlier_dividend_survives():
    """
    The mirror-image bug, and the one that decided the window width. Income from
    before the ledger started exists only as an estimate; deleting it understates a
    Swiss filing aid, which is the worse of the two errors.
    """
    payments = [
        _p(1, date(2026, 1, 5), "yfinance_estimate"),   # a real earlier dividend
        _p(1, date(2026, 2, 9), "yfinance_estimate"),   # the boundary payment's ex-date
        _p(1, BOUNDARY, "ibkr"),
    ]
    kept, _ = DividendService._splice_by_era(payments)

    assert _dates(kept) == [
        (date(2026, 1, 5), "yfinance_estimate"),
        (BOUNDARY, "ibkr"),
    ]


def test_matching_is_one_to_one_so_a_monthly_payer_keeps_its_history():
    """
    A monthly payer's cycle is shorter than the lag window, so a window sweep would
    swallow several months of real income. One IBKR payment consumes at most one
    estimate — the nearest — which is what makes the window safe.
    """
    payments = [
        _p(1, BOUNDARY - timedelta(days=28), "yfinance_estimate"),
        _p(1, BOUNDARY - timedelta(days=14), "yfinance_estimate"),
        _p(1, BOUNDARY - timedelta(days=3), "yfinance_estimate"),   # the duplicate
        _p(1, BOUNDARY, "ibkr"),
    ]
    kept, _ = DividendService._splice_by_era(payments)

    kept_dates = _dates(kept)
    assert (BOUNDARY - timedelta(days=3), "yfinance_estimate") not in kept_dates
    assert (BOUNDARY - timedelta(days=14), "yfinance_estimate") in kept_dates
    assert (BOUNDARY - timedelta(days=28), "yfinance_estimate") in kept_dates


def test_it_does_not_match_across_securities():
    """
    Two securities paying days apart are two dividends. Matching on proximity alone,
    without the security, would silently delete one of them.
    """
    payments = [
        _p(1, date(2026, 2, 9), "yfinance_estimate"),
        _p(2, date(2026, 2, 10), "yfinance_estimate"),
        _p(1, BOUNDARY, "ibkr"),          # only security 1 has an IBKR row
    ]
    kept, _ = DividendService._splice_by_era(payments)

    assert (date(2026, 2, 10), "yfinance_estimate") in _dates(kept)
    assert (date(2026, 2, 9), "yfinance_estimate") not in _dates(kept)


def test_an_estimate_beyond_the_lag_window_is_left_alone():
    """Past the widest real ex-to-pay lag, proximity is no longer evidence."""
    far = BOUNDARY - timedelta(days=EX_TO_PAY_MAX_LAG_DAYS + 1)
    payments = [_p(1, far, "yfinance_estimate"), _p(1, BOUNDARY, "ibkr")]
    kept, _ = DividendService._splice_by_era(payments)
    assert (far, "yfinance_estimate") in _dates(kept)


def test_an_estimate_after_the_boundary_is_still_dropped_by_the_boundary_rule():
    """The original rule must keep working — this adds to it, it does not replace it."""
    payments = [
        _p(1, BOUNDARY + timedelta(days=30), "yfinance_estimate"),
        _p(1, BOUNDARY, "ibkr"),
    ]
    kept, _ = DividendService._splice_by_era(payments)
    assert _dates(kept) == [(BOUNDARY, "ibkr")]


def test_a_history_with_no_ibkr_rows_is_untouched():
    """Before the ledger exists there is no boundary and nothing to deduplicate."""
    payments = [
        _p(1, date(2025, 5, 1), "yfinance_estimate"),
        _p(1, date(2025, 8, 1), "yfinance_estimate"),
    ]
    kept, boundary = DividendService._splice_by_era(payments)
    assert boundary is None
    assert len(kept) == 2


# ── The family rule ───────────────────────────────────────────────────────────

def test_every_reader_of_dividend_rows_goes_through_the_shared_splice():
    """
    The guard that would have caught this class twice.

    `ActivityService._dividends` reimplemented the boundary comparison inline — for a
    good reason, since it windows before splicing and needs the whole-table boundary.
    That copy was correct on the day it was written and silently wrong two days later,
    when the helper gained its boundary-duplicate match: every other reader stopped
    showing the pair and the ledger kept showing it.

    A copy of a rule stays correct only until the rule changes. So the requirement is
    structural: a module that pulls dividend rows out of the repository must also reach
    the one place that decides which of them count. `_splice_by_era` now takes an
    explicit `boundary` precisely so the windowing caller can comply.
    """
    services = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    readers = ("get_between", "get_computed_dividends")
    offenders = []
    for path in sorted(services.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "DividendRepository" not in source:
            continue
        if not any(r in source for r in readers):
            continue
        if "_splice_by_era" not in source:
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} read dividend rows without going through _splice_by_era. "
        "The same payment is stored twice — yfinance under its ex-date, IBKR under its "
        "pay-date — so a reader that skips the splice double-counts every dividend in "
        "the IBKR era."
    )


def test_no_reader_reimplements_the_net_or_income_rules():
    """
    The same requirement for the other two dividend rules, checked the same way.

    `_net_eur` falls back to gross for rows predating the withholding-fields migration
    (its docstring says "every consumer must"), and `_is_income` excludes the zero rows
    yfinance's full history produces. `ActivityService` had inline equivalents of both.
    They agreed to the digit — which is precisely what its inline era-splice copy did,
    right up until the rule changed underneath it.

    So the test is not "do the copies agree" (they did) but "is there a copy at all".
    """
    services = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    # The tell of a local reimplementation: reading the nullable net column directly
    # and choosing a fallback, or testing both money columns for positivity.
    suspicious = (
        "net_amount_eur is not None",
        "net_amount_eur if",
    )
    offenders = []
    for path in sorted(services.glob("*.py")):
        if path.name == "dividend_service.py":
            continue  # the definition lives here
        source = path.read_text(encoding="utf-8")
        for marker in suspicious:
            if marker in source:
                offenders.append(f"{path.name} ({marker!r})")
    assert not offenders, (
        f"{offenders} decide the net-vs-gross fallback locally instead of calling "
        "DividendService._net_eur. A correct copy is still a copy — it stops being "
        "correct the moment the rule moves."
    )
