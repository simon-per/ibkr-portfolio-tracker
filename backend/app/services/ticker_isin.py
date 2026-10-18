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

An unknown suffix resolves nothing: a guess here puts the wrong company's logo on a row.
Pure database; touches no upstream.
"""
from typing import Dict, Iterable, Optional, Tuple

from sqlalchemy import select
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


def normalise(ticker: str) -> str:
    """Upper case, separators dropped, a numeric code's leading zeros stripped."""
    key = "".join(ch for ch in ticker.upper() if ch.isalnum())
    return key.lstrip("0") or key if key.isdigit() else key


def split_yahoo_ticker(yahoo_ticker: str) -> Tuple[str, Optional[Tuple[str, ...]]]:
    """`(normalised base, allowed ISIN countries)`; countries None for an unknown suffix."""
    ticker = yahoo_ticker.strip().upper()
    base, _, suffix = ticker.rpartition(".") if "." in ticker else (ticker, "", "")
    return normalise(base), SUFFIX_COUNTRIES.get(suffix)


async def isins_by_yahoo_ticker(db: AsyncSession, yahoo_tickers: Iterable[str]) -> Dict[str, str]:
    """Upper-cased Yahoo ticker -> the ISIN the stored baskets give it, where one does."""
    wanted: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
    for yahoo_ticker in yahoo_tickers:
        base, countries = split_yahoo_ticker(yahoo_ticker)
        if base and countries:
            wanted[yahoo_ticker.strip().upper()] = (base, countries)
    if not wanted:
        return {}
    bases = {base for base, _ in wanted.values()}
    rows = (await db.execute(
        select(EtfHolding.constituent_ticker, EtfHolding.constituent_isin, EtfHolding.weight_pct)
        .where(EtfHolding.constituent_isin.is_not(None), EtfHolding.constituent_ticker.is_not(None))
    )).all()
    # base -> [(weight, isin)], only for the bases asked about: ~10k rows, normalised in Python
    # because the issuers' separators cannot be matched by one SQL expression.
    by_base: Dict[str, list] = {}
    for ticker, isin, weight in rows:
        key = normalise(ticker)
        if key in bases:
            by_base.setdefault(key, []).append((weight or 0, isin.strip().upper()))
    out: Dict[str, str] = {}
    for yahoo_ticker, (base, countries) in wanted.items():
        matches = [(w, i) for w, i in by_base.get(base, []) if i[:2] in countries]
        if matches:
            out[yahoo_ticker] = max(matches)[1]
    return out
