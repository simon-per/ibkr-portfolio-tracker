"""
What DA-1 can credit: the Swiss treaty residual rate on a portfolio dividend.

A Swiss resident claims foreign withholding back through DA-1, but only up to the rate the
double-taxation treaty lets the source country keep (the residual rate). Withholding above
it is not creditable in Switzerland: it is reclaimed from the source country itself. Summing
the raw withholding per country and labelling it DA-1 overstated the claim for every country
that withholds above its treaty rate (GitHub issue #62). The tax report now splits each
country's recorded withholding, per payment, into ``creditable = min(withheld, gross x
residual)`` and ``excess = withheld - creditable``.

The rates are a private individual's portfolio rate, below every treaty's participation
threshold. **A country is listed only where its rate is known with confidence**, each with
the treaty article it comes from; an unlisted country's row shows no split rather than a
guessed one. Deliberately absent:

- ``CH`` -- Swiss withholding (Verrechnungssteuer) is refunded through the securities
  statement, not DA-1, so no treaty rate applies.
- ``IE`` -- Irish funds withhold nothing, and a zero needs no rate (`creditable_split`).

The reasoning lives in docs/tax.md.
"""
from decimal import Decimal
from typing import Dict, Optional, Tuple

#: Residual rate, in percent of the gross dividend, keyed on ISO 3166 alpha-2.
RESIDUAL_DIVIDEND_RATE_PCT: Dict[str, Decimal] = {
    # Convention of 2 October 1996, art. 10(2).
    "US": Decimal("15"),
    # Convention of 5 May 1997, art. 10(2).
    "CA": Decimal("15"),
    # Abkommen of 11 August 1971, art. 10(2) (Germany withholds 26.375%).
    "DE": Decimal("15"),
    # Convention of 9 September 1966, art. 11(2) (France withholds 12.8% or 25%).
    "FR": Decimal("15"),
    # Convention of 26 February 2010, art. 10(2).
    "NL": Decimal("15"),
    # Convention of 8 December 1977 as amended in 2007, art. 10(2). The UK withholds
    # nothing on ordinary dividends; the rate matters for a REIT distribution.
    "GB": Decimal("15"),
    # Convention of 19 January 1971 as revised in 2010, art. 10(2) (Japan withholds
    # 15.315% on listed shares).
    "JP": Decimal("10"),
    # Convention of 12 February 1980, art. 10(2) (Korea withholds 22%).
    "KR": Decimal("15"),
    # Agreement of 8 October 2007 between the Taipei and Swiss trade offices, art. 10(2)
    # (Taiwan withholds 21%). Added on review, medium confidence: confirm against the
    # ESTV's DA-1 country list before filing (docs/tax.md).
    "TW": Decimal("15"),
}


def residual_rate_pct(country: Optional[str]) -> Optional[Decimal]:
    """The treaty residual rate for `country`, or None when it is not in the table."""
    if not country:
        return None
    return RESIDUAL_DIVIDEND_RATE_PCT.get(country.upper())


def withholding_country(stated: Optional[str], isin: Optional[str]) -> Optional[str]:
    """
    The country that withheld: the one IBKR's withholding line names, else the ISIN's.

    The ISIN prefix is the issuer's domicile, which is not the withholding country for an
    ADR or for some funds -- a Taiwanese company's US-listed ADR has a US ISIN and is
    withheld by Taiwan. A row ingested before the stated country was stored, or one with
    no withholding line, falls back to the prefix.
    """
    if stated:
        return stated.upper()
    prefix = (isin or "")[:2].upper()
    return prefix or None


def creditable_split(
    gross: Decimal, withheld: Decimal, residual_pct: Optional[Decimal]
) -> Optional[Tuple[Decimal, Decimal]]:
    """
    ``(creditable, excess)`` for one payment, or None when no rate is known.

    Withholding of zero or less needs no rate: nothing withheld is nothing to credit and
    nothing to reclaim, whatever the treaty says, so a country missing from the table
    still splits exactly when it withheld nothing.
    """
    if withheld <= 0:
        return withheld, Decimal("0")
    if residual_pct is None:
        return None
    creditable = min(withheld, gross * residual_pct / Decimal("100"))
    return creditable, withheld - creditable
