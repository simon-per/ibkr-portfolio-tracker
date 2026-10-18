"""
A Yahoo ticker's ISIN, read from the stored fund baskets (`etf_holdings`) — for a name we have
never held, which therefore has no `securities` row to carry one (docs/logos.md).

The issuers publish ~10,000 constituents with both a ticker and an ISIN, but each writes the
ticker its own way: Berkshire is "BRK.B" (Vanguard), "BRKB" (iShares US) and "BRK B" (iShares
IE); Tencent is "700" where Yahoo says `0700.HK`; Novo is "NOVO B" for Yahoo's `NOVO-B.CO`.
So both sides are normalised (separators dropped, a numeric code's leading zeros stripped) and
the Yahoo suffix narrows the ISIN's country: a bare ticker is a US listing — which is also where
an ADR's ISIN lives (TSM is US8740391003) — and "2330" is Taiwan's TSMC under `.TW` but also a
Saudi line in one EM basket. Among what is left the heaviest match wins.

A CN ISIN is the one country two of those markets share: an H-share lists in Hong Kong with a
`CNE…` ISIN, as every mainland A-share does, and once the zeros are stripped Vanke's A-share
"000002" is CLP's `0002.HK`. There the code's length as the issuer wrote it decides
(`CN_CODE_DIGITS`): an A-share code is six digits, a Hong Kong code at most five.

An unknown suffix resolves nothing: a guess here puts the wrong company's logo on a row.
Pure database; touches no upstream.
"""
from typing import Dict, Iterable, Optional, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.etf_basket import EtfHolding

#: Yahoo exchange suffix -> the ISIN country prefixes a listing there can carry.
SUFFIX_COUNTRIES: Dict[str, Tuple[str, ...]] = {
    "": ("US",),
    "SZ": ("CN",), "SS": ("CN",),
    "HK": ("HK", "KY", "BM", "CN"),
    "TW": ("TW",), "TWO": ("TW",),
    "KS": ("KR",), "KQ": ("KR",),
    "T": ("JP",),
    "AS": ("NL",), "PA": ("FR",), "DE": ("DE",), "F": ("DE",), "MI": ("IT",), "MC": ("ES",),
    "BR": ("BE",), "VI": ("AT",), "LS": ("PT",), "IR": ("IE",),
    "L": ("GB", "JE", "IE"),
    "SW": ("CH",),
    "ST": ("SE",), "CO": ("DK",), "HE": ("FI",), "OL": ("NO",),
    "TO": ("CA",), "V": ("CA",),
    "AX": ("AU",),
}

#: Suffix -> the digit counts a constituent's code may have, zeros included, for a CN ISIN to be
#: the listing there: mainland A-share codes are six digits, HKEX codes at most five.
CN_CODE_DIGITS: Dict[str, range] = {"HK": range(1, 6), "SZ": range(6, 7), "SS": range(6, 7)}


def _alnum(ticker: str) -> str:
    return "".join(ch for ch in ticker.upper() if ch.isalnum())


def normalise(ticker: str) -> str:
    """Upper case, separators dropped, a numeric code's leading zeros stripped."""
    key = _alnum(ticker)
    return key.lstrip("0") or key if key.isdigit() else key


def _base_and_suffix(yahoo_ticker: str) -> Tuple[str, str]:
    ticker = yahoo_ticker.strip().upper()
    base, _, suffix = ticker.rpartition(".") if "." in ticker else (ticker, "", "")
    return base, suffix


def split_yahoo_ticker(yahoo_ticker: str) -> Tuple[str, Optional[Tuple[str, ...]]]:
    """`(normalised base, allowed ISIN countries)`; countries None for an unknown suffix."""
    base, suffix = _base_and_suffix(yahoo_ticker)
    return normalise(base), SUFFIX_COUNTRIES.get(suffix)


def _candidates(base: str, countries: Tuple[str, ...]):
    """
    SQL superset of the basket lines `base` can match in `countries`; Python decides. The
    separators defeat an equality, but `normalise` only drops characters (and a code's
    leading zeros), so every line that normalises to "BRKB" contains B, R, K, B in that
    order: `LIKE '%B%R%K%B%'`. The ISIN's country is compared as Python reads it. A non-ASCII
    base keeps the country filter only: SQL's `upper` and `LIKE` fold ASCII alone.
    """
    country = func.substr(func.upper(func.trim(EtfHolding.constituent_isin)), 1, 2).in_(countries)
    if not base.isascii():
        return country
    return and_(func.upper(EtfHolding.constituent_ticker).like(f"%{'%'.join(base)}%"), country)


def code_fits(suffix: str, isin: str, constituent_ticker: str) -> bool:
    """False for a CN ISIN whose code, as the issuer wrote it, is not this market's shape."""
    digits = CN_CODE_DIGITS.get(suffix)
    if digits is None or not isin.startswith("CN"):
        return True
    code = _alnum(constituent_ticker)
    return code.isdigit() and len(code) in digits


async def isins_by_yahoo_ticker(db: AsyncSession, yahoo_tickers: Iterable[str]) -> Dict[str, str]:
    """Upper-cased Yahoo ticker -> the ISIN the stored baskets give it, where one does."""
    wanted: Dict[str, Tuple[str, Tuple[str, ...], str]] = {}
    for yahoo_ticker in yahoo_tickers:
        base, countries = split_yahoo_ticker(yahoo_ticker)
        if base and countries:
            suffix = _base_and_suffix(yahoo_ticker)[1]
            wanted[yahoo_ticker.strip().upper()] = (base, countries, suffix)
    if not wanted:
        return {}
    bases = {base for base, _, _ in wanted.values()}
    asked = {(base, countries) for base, countries, _ in wanted.values()}
    rows = (await db.execute(
        select(EtfHolding.constituent_ticker, EtfHolding.constituent_isin, EtfHolding.weight_pct)
        .where(EtfHolding.constituent_isin.is_not(None), EtfHolding.constituent_ticker.is_not(None),
               or_(*(_candidates(base, countries) for base, countries in sorted(asked))))
    )).all()
    # base -> [(weight, isin, ticker as written)], only for the bases asked about. SQL narrows
    # the ~10k basket lines to candidates (`_candidates`); they are normalised in Python because
    # the issuers' separators cannot be matched by one SQL expression.
    by_base: Dict[str, list] = {}
    for ticker, isin, weight in rows:
        key = normalise(ticker)
        if key in bases:
            by_base.setdefault(key, []).append((weight or 0, isin.strip().upper(), ticker))
    out: Dict[str, str] = {}
    for yahoo_ticker, (base, countries, suffix) in wanted.items():
        matches = [(w, i) for w, i, t in by_base.get(base, [])
                   if i[:2] in countries and code_fits(suffix, i, t)]
        if matches:
            out[yahoo_ticker] = max(matches)[1]
    return out
