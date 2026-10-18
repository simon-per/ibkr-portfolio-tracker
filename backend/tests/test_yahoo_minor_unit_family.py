"""
Every path that turns a Yahoo price or dividend into an amount asks one helper what to
divide it by (`app/services/yahoo_minor_units.py`).

Yahoo quotes a `GBp` listing's closes and its dividends in pence. The minor-unit scale was
decided inside `MarketDataService` and only the price path consulted it, so the dividends
of the same chart frame and the dividend pass's `.dividends` read were stored 100x
(GitHub issue #35). **Written against the family, not the instances**: the walk below
finds any module that imports yfinance and reads a frame's `Close` or `Dividends` (or a
ticker's `.dividends`) and requires it to call `minor_unit_scale`, so the next reader is
caught without anyone remembering this file. A module that need not scale is listed in
`EXEMPT` with the reason written down.
"""
import ast
import inspect
import pathlib
from decimal import Decimal

from app.services import yahoo_minor_units
from app.services.dividend_service import DividendService, yahoo_dividend_rows
from app.services.market_data_service import chart_actions
from app.services.yahoo_minor_units import minor_unit_scale
from tests.ast_scope import imports_yfinance

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
HELPER = APP / "services" / "yahoo_minor_units.py"

# Modules that read a Yahoo `Close` and deliberately do not scale it.
EXEMPT = {
    "services/benchmark_service.py": (
        "hand-declared index tickers whose currency is pinned in BENCHMARKS; the "
        "simulation buys units at a close and values them at a close, so a constant "
        "scale cancels, and scaling now would split the cached series at the deploy"
    ),
    "cli/manage_mappings.py": (
        "verifies a mapping against published NAVs and writes no amount: an unscaled "
        "level check refuses (fails closed), the tracking check is a ratio of moves"
    ),
    "services/watchlist_service.py": (
        "its history closes feed only RSI-14, a ratio of moves; nothing stored is a Close"
    ),
}

AMOUNT_COLUMNS = {"Close", "Dividends"}


def _modules():
    for path in sorted(APP.rglob("*.py")):
        yield path.relative_to(APP).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _reads_yahoo_amounts(tree) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in AMOUNT_COLUMNS
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "dividends":
            return True
    return False


def _calls(tree, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                return True
    return False


def _amount_readers():
    return {
        rel: tree for rel, tree in _modules()
        if imports_yfinance(tree) and _reads_yahoo_amounts(tree)
    }


def test_the_walk_finds_the_known_readers():
    """Guards the detector itself: an empty family would pass everything below."""
    readers = _amount_readers()
    assert "services/market_data_service.py" in readers   # Close + chart Dividends
    assert "services/dividend_service.py" in readers      # Ticker.dividends


def test_every_yahoo_amount_reader_asks_the_shared_helper():
    offenders = [
        rel for rel, tree in _amount_readers().items()
        if rel not in EXEMPT and not _calls(tree, "minor_unit_scale")
    ]
    assert offenders == [], (
        f"{offenders} read a Yahoo Close/Dividends without `minor_unit_scale`: a GBp "
        f"listing would be stored 100x. Scale through app/services/yahoo_minor_units.py, "
        f"or add the module to EXEMPT with the reason it need not."
    )


def test_no_exemption_is_stale():
    readers = _amount_readers()
    for rel in EXEMPT:
        assert rel in readers, f"{rel} no longer reads Yahoo amounts; drop its exemption"
        assert not _calls(readers[rel], "minor_unit_scale"), (
            f"{rel} now scales through the helper; drop its exemption"
        )


def test_there_is_no_second_copy_of_the_decision():
    """The maps and the metadata read live in the helper module and nowhere else."""
    copies = []
    for path in sorted(APP.rglob("*.py")):
        if path == HELPER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "MINOR_UNIT_CURRENCIES", "TICKER_CURRENCY_OVERRIDES",
                }:
                    copies.append(f"{path.relative_to(APP).as_posix()}: {target.id}")
            if isinstance(node, ast.Constant) and node.value == "_history_metadata":
                copies.append(f"{path.relative_to(APP).as_posix()}: reads _history_metadata")
    assert copies == []


def test_the_dividend_amount_paths_cannot_omit_the_scale():
    """Required and keyword-only: a reader that forgets fails loudly, not 100x quietly."""
    for function, name in (
        (yahoo_dividend_rows, "scale"),
        (DividendService.store_chart_dividends, "scale"),
    ):
        parameter = inspect.signature(function).parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, function.__name__
        assert parameter.default is inspect.Parameter.empty, function.__name__
    assert inspect.signature(chart_actions).parameters["scale"].default is inspect.Parameter.empty


def test_the_scale():
    assert minor_unit_scale("LLOY.L", "GBp") == Decimal("100")
    assert minor_unit_scale("XYZ.JO", "ZAc") == Decimal("100")
    assert minor_unit_scale("XYZ.TA", "ILA") == Decimal("100")
    assert minor_unit_scale("LLOY.L", "GBP") == Decimal("1")
    assert minor_unit_scale("LLOY.L", None) == Decimal("1")
    # An override is never scaled, whatever Yahoo reports.
    for ticker in yahoo_minor_units.TICKER_CURRENCY_OVERRIDES:
        assert minor_unit_scale(ticker, "GBp") == Decimal("1")
