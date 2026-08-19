"""
PEG from a P/E and a growth estimate — the arithmetic, and that the two services
that report it agree.

The defect: the formula was duplicated in `FundamentalsService` and
`WatchlistService`, and the *resolution orders* had diverged. Both tried Yahoo's
own `pegRatio` first, but the watchlist then tried forward-EPS growth (its own
comment calls that tier "preferred") before the analyst 5-year CAGR, while
fundamentals went straight to the CAGR with **no forward-EPS tier at all**. So one
security could show a PEG on `/api/watchlist` and a different one — or none — on
`/api/fundamentals/portfolio`. Exactly what `ttm_growth` was extracted to end,
between the same two services.

Offline: pure arithmetic plus one structural check, no network.
"""

import inspect

import ast
import pathlib

import pytest

from app.services.peg_ratio import growth_pct_from_estimate, peg_from_growth


class TestGrowthNormalisation:
    def test_a_decimal_estimate_is_scaled_to_a_percent(self):
        # yfinance sends 0.30 meaning 30%.
        assert growth_pct_from_estimate(0.30) == pytest.approx(30.0)

    def test_an_estimate_already_in_percent_is_left_alone(self):
        assert growth_pct_from_estimate(30.0) == pytest.approx(30.0)

    def test_the_boundary_is_one(self):
        assert growth_pct_from_estimate(0.999) == pytest.approx(99.9)
        assert growth_pct_from_estimate(1.0) == pytest.approx(1.0)

    @pytest.mark.parametrize("bad", [None, 0, -0.15, -20])
    def test_a_missing_or_non_positive_estimate_has_no_percent(self, bad):
        """
        The guard that matters: `< 1` is true for *every* negative, so without the
        `<= 0` refusal a −20% estimate already in percent would be scaled to
        −2000%. Both call sites relied on their own `> 0` check for this; it now
        lives with the arithmetic instead of beside it.
        """
        assert growth_pct_from_estimate(bad) is None


class TestPeg:
    def test_divides_the_pe_by_the_growth_percent(self):
        # A P/E of 30 against 30% growth is the textbook PEG of 1.
        assert peg_from_growth(30.0, 0.30) == pytest.approx(1.0)

    def test_reads_a_percent_estimate_the_same_way(self):
        assert peg_from_growth(30.0, 30.0) == pytest.approx(1.0)

    @pytest.mark.parametrize("pe", [None, 0, -12.0])
    def test_refuses_without_a_usable_pe(self, pe):
        # A loss-making company has no meaningful PEG, and a negative one would
        # rank as attractively low wherever PEG is scored.
        assert peg_from_growth(pe, 0.30) is None

    @pytest.mark.parametrize("growth", [None, 0, -0.10])
    def test_refuses_without_a_usable_growth_estimate(self, growth):
        assert peg_from_growth(25.0, growth) is None

    def test_a_tiny_growth_estimate_yields_a_large_peg_rather_than_an_error(self):
        # 25 / 1% = 25. Expensive, but a real answer rather than a division fault.
        assert peg_from_growth(25.0, 0.01) == pytest.approx(25.0)


def _peg_block(func) -> str:
    return inspect.getsource(func)


def test_both_services_resolve_peg_in_the_same_order():
    """
    Structural rather than behavioural, because exercising either service means
    faking a yfinance `.info` frame — but the defect was *order*, and order is
    visible in the source.

    Pins that each service tries forward-EPS growth before the long-term CAGR, and
    that both route through the shared helper rather than reimplementing it.
    """
    from app.services.fundamentals_service import FundamentalsService
    from app.services.watchlist_service import WatchlistService

    for func in (FundamentalsService._extract_metrics, WatchlistService.sync_item):
        src = _peg_block(func)
        assert "peg_from_growth" in src, f"{func.__qualname__} does not use the shared helper"
        assert src.count("peg_from_growth") >= 2, (
            f"{func.__qualname__} has fewer than two PEG fallback tiers — the "
            f"forward-EPS tier is what fundamentals used to be missing"
        )
        fwd = src.index("fwd_eps")
        lt = src.index("longTermGrowth")
        assert fwd < lt, (
            f"{func.__qualname__} consults longTermGrowth before forward EPS; the "
            f"watchlist calls the forward-EPS tier the preferred one, so both must "
            f"try it first or the two endpoints disagree"
        )


def test_neither_service_still_normalises_growth_inline():
    """The duplicated `* 100 if < 1` heuristic must not come back beside the shared one."""
    from app.services import fundamentals_service, watchlist_service

    for module in (fundamentals_service, watchlist_service):
        src = inspect.getsource(module)
        assert "* 100 if" not in src, (
            f"{module.__name__} normalises a growth estimate inline again — that is "
            f"the duplication peg_ratio.py exists to remove"
        )


# ---------------------------------------------------------------------------
# The decimal-vs-percent inference is wrong in the direction that matters.
#
# `growth_estimates['+1y']['stockTrend']` is a decimal fraction that legitimately exceeds
# 1 whenever a company is expected to grow more than 100%. The inference read anything
# >= 1 as already-a-percent, so 222% growth became 2.2245% and a PEG of 0.25 became
# 24.56 — a factor of 100, in the direction that makes a fast grower look catastrophically
# overvalued. Three of this account's watchlist rows carry such a value today.
#
# That the column is a decimal is not a judgement call: the same value is stored as
# `fwd_eps_growth` and rendered by the UI as `(v * 100).toFixed(1)%`.
# ---------------------------------------------------------------------------


def test_a_fraction_above_one_is_not_mistaken_for_a_percent():
    # SNDK, live: P/E 54.64, +1y stockTrend 2.2245 (i.e. 222% growth).
    assert peg_from_growth(54.64, 2.2245, is_fraction=True) == pytest.approx(54.64 / 222.45)
    # The inference, left to itself, is 100x out.
    assert peg_from_growth(54.64, 2.2245) == pytest.approx(54.64 / 2.2245)


def test_the_scoring_consequence_of_getting_that_wrong():
    """A 10-point swing on the buy score's 25-point valuation block."""
    ladder = lambda p: 10 if p <= 0.5 else 8 if p <= 1 else 6 if p <= 1.5 else 4 if p <= 2 else 2 if p <= 3 else 0
    right = peg_from_growth(54.64, 2.2245, is_fraction=True)
    wrong = peg_from_growth(54.64, 2.2245)
    assert ladder(right) == 10
    assert ladder(wrong) == 0


def test_a_known_fraction_below_one_is_unchanged_by_the_flag():
    """The common case must be identical either way, or the flag would be a second rule."""
    for growth in (0.05, 0.15, 0.30, 0.999):
        assert peg_from_growth(20.0, growth, is_fraction=True) == peg_from_growth(20.0, growth)


def test_the_long_term_tier_keeps_inferring():
    """
    `longTermGrowth` has no established convention in this codebase, so its tier keeps the
    inference rather than assuming. The flag is opt-in precisely so one tier can know its
    input while the other admits it does not.
    """
    assert growth_pct_from_estimate(15) == 15          # read as a percent
    assert growth_pct_from_estimate(0.15) == pytest.approx(15)   # read as a fraction


def test_a_non_positive_growth_is_still_refused_under_the_flag():
    for bad in (None, 0, -0.2):
        assert growth_pct_from_estimate(bad, is_fraction=True) is None
        assert peg_from_growth(20.0, bad, is_fraction=True) is None


# ── The family rule ─────────────────────────────────────────────────────────


class _Item:
    """The two columns `_compute_peg` reads, plus the stored value it prefers."""

    def __init__(self, trailing_pe, fwd_eps_growth, peg_ratio=None):
        self.trailing_pe = trailing_pe
        self.fwd_eps_growth = fwd_eps_growth
        self.peg_ratio = peg_ratio


def test_the_read_time_peg_refuses_a_loss_making_company():
    """
    `routers/watchlist.py::_compute_peg` was a fourth hand-written PEG — written after
    the watchlist's and the fundamentals service's copies had been extracted into
    `peg_ratio.py` precisely to stop that, and left behind by the extraction.

    Its arithmetic was a faithful copy of the forward-EPS tier. Its *guard* was not:
    `if item.trailing_pe` is a truthiness test, so a negative P/E sailed through where
    `peg_from_growth` refuses `trailing_pe <= 0` on the stated grounds that a
    loss-making company has no meaningful PEG.

    The result is the dangerous kind of wrong — not a blank, a number. On a PEG scale
    **-0.30** reads as spectacularly cheap. And the NULL `peg_ratio` column that makes
    this function fire at all is exactly the state such a company is in, because both
    fallbacks in the sync path correctly refused it: the read path resurrected precisely
    what the write path had declined to store.
    """
    from app.routers.watchlist import _compute_peg

    assert _compute_peg(_Item(-15.0, 0.5)) is None
    assert peg_from_growth(-15.0, 0.5, is_fraction=True) is None


def test_the_read_time_peg_still_answers_the_ordinary_case():
    """The refusal must not have taken the feature with it."""
    from app.routers.watchlist import _compute_peg

    assert _compute_peg(_Item(30.0, 0.5)) == pytest.approx(0.6)
    # A stored value always wins; this only fills a NULL.
    assert _compute_peg(_Item(30.0, 0.5, peg_ratio=1.23)) == 1.23
    assert _compute_peg(_Item(30.0, None)) is None
    assert _compute_peg(_Item(30.0, 0.0)) is None


def test_the_read_time_peg_rounds_like_the_sync_path():
    """
    The sync path wraps `peg_from_growth` in `_safe_float`, which rounds to 4dp. A
    derived PEG rounding differently from a stored one is the `_safe_float` divergence
    CLAUDE.md records — one ratio reading differently on two screens.
    """
    from app.routers.watchlist import _compute_peg

    derived = _compute_peg(_Item(30.0, 0.37))
    assert derived == round(derived, 4)


def test_nobody_computes_peg_without_the_shared_helper():
    """
    The family form, so the *fifth* copy is caught rather than these four.

    Any module dividing a P/E by a growth figure is computing a PEG and must reach
    `peg_ratio.peg_from_growth`, which owns the two rules that keep being lost: refuse a
    non-positive P/E, and know whether the growth input is a fraction or a percent.
    Routers are in scope here precisely because that is where the last one hid —
    `test_fundness_predicate.py` scans only `app/services/` and could not have seen it.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in sorted(list((root / "services").glob("*.py")) + list((root / "routers").glob("*.py"))):
        if path.name == "peg_ratio.py":
            continue
        source = path.read_text(encoding="utf-8")
        code = chr(10).join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        if "peg" not in code.lower():
            continue
        # A division whose left side mentions a P/E and whose right mentions growth is
        # a PEG by any other name.
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            left = ast.dump(node.left).lower()
            right = ast.dump(node.right).lower()
            if ("_pe" in left or "pe_" in left) and "growth" in right:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"a hand-rolled PEG reappeared at {offenders}; call peg_ratio.peg_from_growth, "
        "which refuses a non-positive P/E and knows the growth convention"
    )
