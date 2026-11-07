"""
How yfinance reports a failure: raised, never swallowed (review item Y2, 2026-11-07).

yfinance 1.1 ships with `yf.config.debug.hide_exceptions = True`, and under it several
scrapers catch **everything** — `quarterly_financials` (`_create_financials_table`'s bare
`except Exception: pass`) among them. A 429 on that endpoint came back as an empty frame:
`read_optional` never saw it, the hold was not stamped, the pass asked the next company,
and `.info`'s `earningsGrowth` was stored as if the statements had been measured. Rule 1
(CLAUDE.md) broken by the library, not by us, which is why every stub in
`tests/test_yahoo_estimates.py` (which raises) missed it.

`raise_yahoo_errors()` turns the flag off. It is called from `check_yahoo_hold()`, which
every module importing yfinance awaits before it asks
(`tests/test_yahoo_rate_limit_family.py`), so the setting is in force on every path by
construction, a CLI included, without an entry point to remember. Idempotent and local:
one attribute on yfinance's process-wide config, no request.

With the flag off yfinance also *raises* where it used to hand back an empty frame for a
ticker it has no data for — `YFPricesMissingError`, `YFTzMissingError` (both
`YFTickerMissingError`). The callers that read "empty" as an answer ("no closes in the
window", "no dividends") go through `history_or_empty`, so a delisted ticker or a long
halt stays a no-data answer and does not turn into an error count. Everything else —
a 404 on `.info`, a frame yfinance cannot parse — now reaches the caller's own handler as
the error it is.

Imports yfinance's exception classes only; it makes no request
(`NOT_IN_THE_FAMILY` in the family test says so).
"""

from typing import Any, Callable

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFTickerMissingError


def raise_yahoo_errors() -> None:
    """Make yfinance raise what it meets (a 429 above all) instead of hiding it."""
    yf.config.debug.hide_exceptions = False


def is_no_data(error: BaseException) -> bool:
    """Yahoo answered and has nothing for this ticker (delisted, unknown, a halt)."""
    return isinstance(error, YFTickerMissingError)


def history_or_empty(read: Callable[[], Any]) -> Any:
    """
    `read()` — a `history(...)`, or a property built on it such as `.dividends` — or an
    empty frame when Yahoo has no data for the ticker. Anything else propagates, a rate
    limit above all.
    """
    try:
        return read()
    except YFTickerMissingError:
        return pd.DataFrame()
