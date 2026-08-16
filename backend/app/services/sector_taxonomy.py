"""
One canonical sector vocabulary, and the map from every spelling the providers use into it.

**This exists because the app already had four sector taxonomies and no normaliser.** Yahoo writes
`securities.sector` in Morningstar's names (`Technology`, `Financial Services`); iShares writes
GICS-ish names into `etf_holdings.sector` (`Information Technology`, `Financials`, and — measured, not
guessed — `Communication` with no "Services"); Xtrackers writes its own (`Telecommunications`); and
`AllocationTab.tsx` renamed a handful of them on the client, *after* the API boundary, which is why the
backend could stay unaware that any of it disagreed.

`CANONICAL_SECTORS` is deliberately **the set that tab already displays**, not GICS. Adopting GICS
would have been defensible on coverage grounds — iShares answers 97 of the 103 ISINs behind this
account's top companies against Yahoo's 27 — but it would rename every bucket on a screen that has
been showing "Technology" for months, to no reader's benefit.

**Scope, because this crosses a refusal written down elsewhere.** `models/etf_basket.py` calls
`etf_holdings.sector` "deliberately UNSERVED", on the grounds that serving it would put a second
sector answer beside `etf_mappings.py`'s on the Allocation tab. That still holds and the Allocation
tab is untouched: this module exists so the *look-through* can group the companies it already draws,
per company, with no portfolio-level rollup anywhere in the response. The two screens answer
different questions over different denominators — one covers the whole book from hand-maintained
fund-level percentages, the other covers only what could be decomposed — and neither is a rollup of
the other.

Adding a provider means adding its spellings here, never adding a second map somewhere else.
"""
from typing import Dict, Optional

# The bucket for "we could not tell". A real value, not an absence: a company dropped from the
# chart for want of a sector would silently shrink the whole, which is the failure
# `allocation_service` fixed in 2026-08-05 by writing `or 'Unknown'` instead of skipping the row.
UNKNOWN = "Unknown"

# The eleven the app already displays, in a fixed order. The order is load-bearing: it is what
# `sectorColors.ts` assigns hues by, so a sector keeps its colour no matter which sectors the
# portfolio happens to hold this week.
CANONICAL_SECTORS = (
    "Technology",
    "Communications",
    "Financials",
    "Consumer Cyclical",
    "Healthcare",
    "Industrials",
    "Consumer Defensive",
    "Energy",
    "Basic Materials",
    "Real Estate",
    "Utilities",
)

# Every spelling observed in this account's own data, plus the obvious neighbours. Keys are
# lower-cased and stripped before lookup, so case and padding need no entries of their own.
_ALIASES: Dict[str, str] = {
    # --- Technology ---
    "technology": "Technology",
    "information technology": "Technology",          # iShares / GICS
    "tech": "Technology",
    "it": "Technology",
    # --- Communications ---
    "communications": "Communications",
    "communication": "Communications",               # iShares, measured — note: no "Services"
    "communication services": "Communications",      # Yahoo / GICS
    "telecommunications": "Communications",          # Xtrackers
    "telecommunication": "Communications",
    "media": "Communications",
    # --- Financials ---
    "financials": "Financials",
    "financial services": "Financials",              # Yahoo
    "finance": "Financials",
    "financial": "Financials",
    # --- Consumer Cyclical ---
    "consumer cyclical": "Consumer Cyclical",        # Yahoo
    "consumer discretionary": "Consumer Cyclical",   # GICS
    # --- Consumer Defensive ---
    "consumer defensive": "Consumer Defensive",      # Yahoo
    "consumer staples": "Consumer Defensive",        # GICS
    # --- Healthcare ---
    "healthcare": "Healthcare",
    "health care": "Healthcare",                     # GICS
    # --- the rest, where the two taxonomies already agree ---
    "industrials": "Industrials",
    "energy": "Energy",
    "basic materials": "Basic Materials",            # Yahoo
    "materials": "Basic Materials",                  # GICS
    "real estate": "Real Estate",
    "utilities": "Utilities",
}

# Values that are *present* in a provider's file and mean "no classification". Mapping these to
# UNKNOWN rather than letting them fall through keeps them out of the alias table's miss path,
# where a future audit of unmapped strings would otherwise keep rediscovering them.
#
# The polarity here is worth knowing: the DWS parser already nulls its own literal `'unknown'` at
# the adapter boundary (`etf_basket_parsers.py`), while `allocation_service` manufactures the
# string `'Unknown'` for a NULL column. One layer erases it and the other creates it, so this
# module has to accept both a None and the word itself and answer the same way.
_EXPLICIT_UNKNOWN = frozenset({
    "unknown",
    "other",              # iShares uses this literally; it carries no information
    "unclassified",
    "n/a",
    "na",
    "none",
    "-",
    "",
})


def normalise(raw: Optional[str]) -> str:
    """
    A provider's sector string as one of `CANONICAL_SECTORS`, or `UNKNOWN`.

    Never raises and never returns an unseen name: a spelling nobody has mapped becomes `UNKNOWN`
    rather than its own bucket, because an unmapped string on a treemap is a one-company group with
    a provider's private vocabulary printed on it. `unmapped_sectors()` is how those get found.
    """
    if raw is None:
        return UNKNOWN
    key = " ".join(str(raw).strip().lower().split())
    if key in _EXPLICIT_UNKNOWN:
        return UNKNOWN
    return _ALIASES.get(key, UNKNOWN)


def is_canonical(name: str) -> bool:
    return name in CANONICAL_SECTORS


def unmapped_sectors(raws) -> set:
    """
    The distinct provider spellings that fell through to `UNKNOWN` without being declared unknown.

    For diagnostics and for the test that walks production's own distinct values: a provider
    renaming `Health Care` to `Healthcare Equipment` would otherwise show up only as a quietly
    growing Unknown bucket, which looks like missing data rather than a missing alias.
    """
    missing = set()
    for raw in raws:
        if raw is None:
            continue
        key = " ".join(str(raw).strip().lower().split())
        if key in _EXPLICIT_UNKNOWN or key in _ALIASES:
            continue
        missing.add(str(raw).strip())
    return missing
