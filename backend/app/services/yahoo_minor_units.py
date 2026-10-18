"""
One answer to "what does this Yahoo response's amount have to be divided by", shared by
every path that turns a Yahoo price or dividend into a stored amount.

Yahoo quotes some listings in a currency's MINOR unit and says so in the code it reports:
London equities in `GBp` (pence), Johannesburg in `ZAc`, Tel Aviv in `ILA`. Its dividend
events for those listings are in the same unit. The row's *label* is still the major code,
so the *amount* has to be divided or a 5-pound price — or a 4.5p dividend — is stored 100x.

This lived inside `MarketDataService` and only the price path consulted it: the dividends
of the very same chart frame, and the dividend pass's own `.dividends` read, were stored
as Yahoo reported them (GitHub issue #35). Hence one module, consulted by the price path
(`MarketDataService._try_fetch_yahoo`), both dividend readers
(`dividend_service.yahoo_dividend_rows` takes the scale) and `market_cap`.
`tests/test_yahoo_minor_unit_family.py` fails if a module reads a Yahoo `Close` or
dividend without asking here.

Uppercasing `GBp` to `GBP` without scaling **defeats the currency guard** rather than
missing a conversion: the normalised code matches the security's own currency, so the
"reject a variation whose currency disagrees" check passes and nothing downstream can see
a position carried 100x high. That is the SBI failure with its one safeguard removed,
which is why this is a scale and not just a rename.
"""
from decimal import Decimal
from typing import Any, Optional, Tuple

# Tickers where the automatic currency answer is wrong, checked by hand: SMH.L trades in
# USD on London, not GBP. An override is also never scaled — someone read the listing, so
# the code Yahoo reports is not evidence.
TICKER_CURRENCY_OVERRIDES = {
    'SMH.L': 'USD',
}

# Explicit rather than inferred from letter case: `GBp` and `ZAc` are mixed-case but `ILA`
# is not, so "not all-uppercase means minor unit" would silently miss Tel Aviv. Extend the
# map when a new venue appears, the way `MarketDataService.EXCHANGE_SUFFIXES` is extended.
MINOR_UNIT_CURRENCIES = {
    'GBp': ('GBP', Decimal('100')),
    'ZAc': ('ZAR', Decimal('100')),
    'ILA': ('ILS', Decimal('100')),
}

NO_SCALE = Decimal('1')


def minor_unit(reported_currency: Optional[str]) -> Optional[Tuple[str, Decimal]]:
    """`('GBP', Decimal(100))` when the code is a minor unit, else None."""
    return MINOR_UNIT_CURRENCIES.get((reported_currency or '').strip())


def minor_unit_scale(ticker: str, reported_currency: Optional[str]) -> Decimal:
    """
    What every amount in this ticker's Yahoo response must be divided by: the minor unit's
    divisor, or 1 when the report is a major code, is missing, or the ticker is a
    `TICKER_CURRENCY_OVERRIDES` entry. Read off the *reported* code — a normalised label
    can no longer tell pence from pounds.
    """
    if ticker in TICKER_CURRENCY_OVERRIDES:
        return NO_SCALE
    minor = minor_unit(reported_currency)
    return minor[1] if minor else NO_SCALE


def reported_currency_of(yf_ticker: Any) -> Optional[str]:
    """
    The currency Yahoo stated in the metadata of the response this `yf.Ticker` just
    received, at no extra request, or None.

    `history()` and `.dividends` both fill it: yfinance's `get_dividends` is
    `history(period="max")` on the same `PriceHistory` object, which stores the chart
    endpoint's `meta` as `_history_metadata`. Deliberately NOT the public
    `history_metadata` property: it re-requests at an intraday interval whenever
    'tradingPeriods' is missing, which a daily history never populates — that would
    double our Yahoo traffic to two requests per security. Read the cached dict instead,
    and degrade to None (no scaling, suffix inference for the label) if yfinance moves it.
    """
    try:
        meta = getattr(yf_ticker._price_history, '_history_metadata', None)
        return (meta or {}).get('currency')
    except Exception:
        return None
