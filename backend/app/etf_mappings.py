"""
Manual ETF allocation mappings for sector and geographic breakdown.
These percentages are approximate based on ETF holdings and methodology.

**The two dimensions are not equally well sourced, and the entries say which is which.**
Yahoo's `Ticker(...).funds_data.sector_weightings` returns a real measured sector split for
a fund, so a `sector` block can be copied from it and dated. It exposes **no country or
region weights at all** (`equity_holdings` is valuation ratios, `asset_classes` is
stock/bond/cash) — so every `geographic` block here is hand-derived, and the best available
evidence is `top_holdings`, whose usefulness depends entirely on how much of the fund those
ten names cover. That coverage is recorded per entry, because it is the difference between
a measurement and a guess: GRID's top ten is 59.6% of the fund, QTUM's is 14.9%.

Do not "improve" a geographic block from an ETF's *sector* data or from `.info` — a fund
carries no `country`, which is exactly why the look-through table exists.

**`isins` per entry exists because a symbol is not an identity, and this table was
already wrong about one fund.** `securities` identity is `isin + exchange`, symbols are
not unique, and `SMH` is the live collision: this account holds the **UCITS** VanEck
Semiconductor fund (`IE00BMC38736`, LSE, USD) while the far better-known `SMH` is the
US-listed VanEck fund — a symbol-keyed lookup cannot tell them apart, and the two are
different instruments with different domiciles. So every entry declares the ISINs it
covers, and `fund_isins()` / `is_known_etf_isin()` / `symbol_for_fund_isin()` below are
**the** fundness predicates. CLAUDE.md records that this predicate has already diverged
once (`securities.asset_type` said "Stock" while this table said "ETF"); a third answer
would be the same bug a third time, so nothing outside this module may decide it.
`tests/test_etf_source_registry.py::test_every_declared_source_is_a_known_fund` holds the
line the one place it could realistically be crossed, by requiring `FUND_SOURCES` and
`ETF_ALLOCATIONS` to name the same ISINs in **both** directions. (Earlier revisions of
this docstring cited a `tests/test_fund_predicate_family.py` that has never existed — a
claimed guard is worse than an acknowledged gap, since it stops anyone looking.)

**The successor to the two percent blocks is `etf_holdings`, and the precondition for
migrating is written down here so the two do not silently diverge.** Real constituent
files carry a sector and a country of risk *per holding*, so once every held fund has a
stored basket the `sector` and `geographic` blocks here become measurable rather than
estimated. That switch is deliberately not made yet, and one of its three original
blockers is now cleared:

- coverage is **11 of 12 funds** (was 6), but VWCE — 9.2% of the book — reaches that only
  by borrowing VT's basket, and DBPG is excluded outright. So the Allocation tab's numbers
  would start depending on whether a scraper succeeded, and one of them would be an
  approximation presented in a table that states none;
- `countryOfRisk` is a **country** while `geographic` buckets are **regions**, so
  switching needs a new hand-maintained country-to-region map — a new drift surface
  rather than a removed one;
- ~~a third sector taxonomy~~ **resolved**: `app/services/sector_taxonomy.py` normalises
  Yahoo's, iShares', DWS's and First Trust's spellings into the eleven names
  `AllocationTab.tsx` already displays, and the look-through view groups on it.

Until the first two are resolved, **this table is the live answer for the three
allocation charts** and `lookthrough_service` says so from its own side — it serves a
per-company sector for *grouping* and publishes no sector total anywhere.
"""

ETF_ALLOCATIONS = {
    # iShares Core MSCI World (IWDA)
    "IWDA": {
        "isins": ["IE00B4L5Y983"],
        "asset_type": "ETF",
        "geographic": {
            "North America": 72.0,
            "Europe": 15.0,
            "Asia Pacific": 10.0,
            "Emerging Markets": 3.0,
        },
        "sector": {
            "Technology": 23.0,
            "Financial Services": 14.0,
            "Healthcare": 12.0,
            "Consumer Cyclical": 11.0,
            "Industrials": 10.0,
            "Communication Services": 8.0,
            "Consumer Defensive": 7.0,
            "Energy": 5.0,
            "Real Estate": 3.0,
            "Utilities": 3.0,
            "Basic Materials": 4.0,
        },
    },

    # Vanguard FTSE All-World (VWCE)
    "VWCE": {
        "isins": ["IE00BK5BQT80"],
        "asset_type": "ETF",
        "geographic": {
            "North America": 65.0,
            "Europe": 16.0,
            "Asia Pacific": 12.0,
            "Emerging Markets": 7.0,
        },
        "sector": {
            "Technology": 22.0,
            "Financial Services": 15.0,
            "Healthcare": 12.0,
            "Consumer Cyclical": 11.0,
            "Industrials": 11.0,
            "Communication Services": 7.0,
            "Consumer Defensive": 6.0,
            "Energy": 5.0,
            "Basic Materials": 5.0,
            "Real Estate": 3.0,
            "Utilities": 3.0,
        },
    },

    # iShares Core S&P 500 (SXR8)
    "SXR8": {
        "isins": ["IE00B5BMR087"],
        "asset_type": "ETF",
        "geographic": {
            "United States": 100.0,
        },
        "sector": {
            "Technology": 30.0,
            "Financial Services": 13.0,
            "Healthcare": 12.0,
            "Consumer Cyclical": 11.0,
            "Communication Services": 9.0,
            "Industrials": 8.0,
            "Consumer Defensive": 6.0,
            "Energy": 4.0,
            "Real Estate": 3.0,
            "Utilities": 2.0,
            "Basic Materials": 2.0,
        },
    },

    # Vanguard S&P 500 (VOO) — **not held.** Present only because `etf_sources.py` declares
    # it as DBPG's basket proxy, and `test_etf_source_registry.py` requires the two tables to
    # name the same ISINs in both directions. Both blocks are deliberately identical to SXR8
    # and DBPG above: all three track the S&P 500, and three entries for one index disagreeing
    # in this table would be the dominant failure mode in miniature. Change one, change all
    # three. Nothing reads these while the fund is unheld — they exist so that buying it
    # cannot land it in an *Unknown* bucket.
    "VOO": {
        "isins": ["US9229083632"],
        "asset_type": "ETF",
        "geographic": {
            "United States": 100.0,
        },
        "sector": {
            "Technology": 30.0,
            "Financial Services": 13.0,
            "Healthcare": 12.0,
            "Consumer Cyclical": 11.0,
            "Communication Services": 9.0,
            "Industrials": 8.0,
            "Consumer Defensive": 6.0,
            "Energy": 4.0,
            "Real Estate": 3.0,
            "Utilities": 2.0,
            "Basic Materials": 2.0,
        },
    },

    # Xtrackers NASDAQ 100 (XNAS)
    "XNAS": {
        "isins": ["IE00BMFKG444"],
        "asset_type": "ETF",
        "geographic": {
            "United States": 100.0,
        },
        "sector": {
            "Technology": 55.0,
            "Communication Services": 18.0,
            "Consumer Cyclical": 15.0,
            "Healthcare": 6.0,
            "Industrials": 4.0,
            "Consumer Defensive": 2.0,
        },
    },

    # Xtrackers S&P 500 2x Leveraged (DBPG)
    "DBPG": {
        "isins": ["LU0411078552"],
        "asset_type": "ETF",
        "geographic": {
            "United States": 100.0,
        },
        "sector": {
            "Technology": 30.0,
            "Financial Services": 13.0,
            "Healthcare": 12.0,
            "Consumer Cyclical": 11.0,
            "Communication Services": 9.0,
            "Industrials": 8.0,
            "Consumer Defensive": 6.0,
            "Energy": 4.0,
            "Real Estate": 3.0,
            "Utilities": 2.0,
            "Basic Materials": 2.0,
        },
    },

    # VanEck Semiconductor UCITS ETF (SMH) — IE00BMC38736, Irish-domiciled, LSE-listed,
    # USD. NOT the US-listed VanEck SMH, which is the one every search result describes.
    # Both track the same MVIS semiconductor index, so the blocks below hold for either,
    # but the ISIN is what tells them apart and the holdings files are per-ISIN.
    "SMH": {
        "isins": ["IE00BMC38736"],
        "asset_type": "ETF",
        "geographic": {
            "United States": 60.0,
            "Taiwan": 20.0,
            "Netherlands": 10.0,
            "South Korea": 10.0,
        },
        "sector": {
            "Technology": 100.0,
        },
    },

    # Invesco PHLX Semiconductor ETF (SOXQ)
    #
    # Held since 2026-07-27 and previously unmapped, which is worse than it looks:
    # without an entry here `is_known_etf` is False, so the position falls through
    # to `security.asset_type` — whose column default is "Stock" — and to
    # `security.sector`/`security.country`, which Yahoo leaves empty for a fund.
    # The result was a holding absent from the sector *and* geographic treemaps
    # and counted as a stock in the third, with no sync able to fix it.
    #
    # Approximate, as everywhere in this file. Sector is unambiguous for a pure
    # semiconductor fund; the geography skews more US than SMH because the PHLX
    # SOX index is limited to US-listed names. Adjust if you want it tighter.
    "SOXQ": {
        "isins": ["US46138G6153"],
        "asset_type": "ETF",
        "geographic": {
            "United States": 80.0,
            "Taiwan": 10.0,
            "Netherlands": 8.0,
            "South Korea": 2.0,
        },
        "sector": {
            "Technology": 100.0,
        },
    },

    # Xtrackers Artificial Intelligence & Big Data (XAIX)
    "XAIX": {
        "isins": ["IE00BGV5VN51"],
        "asset_type": "ETF",
        "geographic": {
            "United States": 85.0,
            "Europe": 10.0,
            "Asia Pacific": 5.0,
        },
        "sector": {
            "Technology": 90.0,
            "Communication Services": 7.0,
            "Industrials": 3.0,
        },
    },

    # iShares Core MSCI Emerging Markets IMI (EMIM)
    "EMIM": {
        "isins": ["IE00BKM4GZ66"],
        "asset_type": "ETF",
        "geographic": {
            "China": 30.0,
            "India": 20.0,
            "Taiwan": 17.0,
            "Brazil": 7.0,
            "Saudi Arabia": 5.0,
            "South Africa": 4.0,
            "Other Emerging Markets": 17.0,
        },
        "sector": {
            "Technology": 22.0,
            "Financial Services": 21.0,
            "Consumer Cyclical": 14.0,
            "Communication Services": 10.0,
            "Energy": 8.0,
            "Basic Materials": 8.0,
            "Industrials": 6.0,
            "Healthcare": 5.0,
            "Consumer Defensive": 4.0,
            "Utilities": 2.0,
        },
    },

    # Vanguard Total World Stock (VT) — bought 2026-08-06.
    # Sector: Yahoo funds_data, 2026-08-06 (Technology carries the +0.01 rounding).
    # Geographic: deliberately identical to VWCE above. VT tracks FTSE Global All Cap and
    # VWCE tracks FTSE All-World — the same index family, differing by small-cap inclusion,
    # which barely moves a regional weight. Two funds on one index disagreeing in this
    # table would be this codebase's dominant failure mode in miniature, so they are
    # pinned together on purpose: change one and change the other.
    # `etf_sources.py` now makes the same judgement one level down — VWCE borrows VT's
    # constituent basket outright — so if the two ever stop being interchangeable, both
    # places have to move, not just this one.
    "VT": {
        "isins": ["US9220427424"],
        "asset_type": "ETF",
        "geographic": {
            "North America": 65.0,
            "Europe": 16.0,
            "Asia Pacific": 12.0,
            "Emerging Markets": 7.0,
        },
        "sector": {
            "Technology": 31.19,
            "Financial Services": 15.72,
            "Industrials": 11.74,
            "Consumer Cyclical": 8.99,
            "Healthcare": 8.31,
            "Communication Services": 7.4,
            "Consumer Defensive": 4.53,
            "Basic Materials": 3.81,
            "Energy": 3.55,
            "Utilities": 2.48,
            "Real Estate": 2.28,
        },
    },

    # First Trust NASDAQ Clean Edge Smart Grid Infrastructure (GRID) — bought 2026-08-06.
    # Sector: Yahoo funds_data, 2026-08-06. Its 0.01 Basic Materials sliver is folded into
    # Industrials rather than kept — a 0.01% category is noise that renders as a treemap
    # tile indistinguishable from a real one.
    # Geographic: derived from top_holdings, which covers **59.6%** of the fund — the
    # strongest basis of the three. Within it the split is 30.2 North America
    # (JCI/ETN/PWR/NVT/HUBB) against 29.4 Europe (Schneider FR, ABB CH, E.ON DE,
    # National Grid GB, Prysmian IT), i.e. almost exactly even. Note JCI, ETN and NVT are
    # Irish plcs counted by their **US listing**, consistent with the rest of this table
    # and with `currencyExposure.ts`: where a thing trades is what we can actually observe.
    # The tail is given a little Asia Pacific, which the measured decile has none of.
    "GRID": {
        "isins": ["US33737A1088"],
        "asset_type": "ETF",
        "geographic": {
            "North America": 48.0,
            "Europe": 42.0,
            "Asia Pacific": 10.0,
        },
        "sector": {
            "Industrials": 66.46,
            "Utilities": 18.76,
            "Technology": 11.5,
            "Consumer Cyclical": 3.28,
        },
    },

    # Defiance Quantum (QTUM) — bought 2026-08-06.
    # Sector: Yahoo funds_data, 2026-08-06.
    # Geographic: the weakest block in this file, and flagged rather than dressed up. The
    # top ten covers only **14.9%** of the fund, so it is a thin sample — mitigated, but
    # only partly, by the fund being near equal-weighted (every top holding sits in
    # 1.41-1.62%), which makes that decile more representative of the whole than a
    # cap-weighted fund's would be. Within it: 12.0 North America, 1.5 Asia Pacific
    # (NEC 6701.T), 1.4 Europe (Airbus AIR.PA). The tail of a quantum-computing index
    # carries more Japanese and European names than the head does, so Asia Pacific and
    # Europe are set above their measured share deliberately.
    # If this matters later, replace it with Defiance's own published country weights;
    # that is a real source and this is an estimate.
    "QTUM": {
        "isins": ["US26922A4206"],
        "asset_type": "ETF",
        "geographic": {
            "North America": 65.0,
            "Asia Pacific": 20.0,
            "Europe": 15.0,
        },
        "sector": {
            "Technology": 77.75,
            "Industrials": 10.95,
            "Communication Services": 7.16,
            "Consumer Cyclical": 2.76,
            "Healthcare": 1.38,
        },
    },
}


def get_etf_allocation(symbol: str) -> dict:
    """
    This table's own row for a symbol, or None.

    **Not a fundness predicate for a holding.** It answers "does the table have a row
    under this label", which is only the same question when the label is an identity —
    and a ticker is not. Reach a *holding's* row through `allocation_for_fund_isin`;
    this exists for that function and for tests over the table itself.
    """
    return ETF_ALLOCATIONS.get(symbol)


def is_known_etf(symbol: str) -> bool:
    """
    Does the table have a row under this symbol?

    **Not the fundness predicate — use `is_known_etf_isin`.** A symbol is not an
    identity: this account holds the *UCITS* VanEck fund `IE00BMC38736`, and the far
    better-known US `SMH` shares its ticker, so a symbol lookup cannot tell them apart
    and would hand one fund the other's sector and region split. Worse, a *stock* whose
    ticker collides with a row here would be distributed across eleven sectors as though
    it were a fund — a fabricated allocation rather than a caveat.

    Kept for lookups over the table itself (and because `symbol_for_fund_isin` is the
    inverse), never for classifying a security. `tests/test_fundness_predicate.py` fails
    any service that calls this on a holding.
    """
    return symbol in ETF_ALLOCATIONS


def _build_isin_index() -> dict:
    """
    ISIN -> symbol, derived from the table rather than maintained beside it.

    A second literal listing the same ISINs is exactly the duplication this codebase
    keeps being bitten by, so the index is computed once at import. A duplicate ISIN
    across two entries raises here — at import, not on a request — because two funds
    sharing an ISIN is a copy-paste slip that would otherwise silently give one fund
    the other's basket.
    """
    index = {}
    for symbol, entry in ETF_ALLOCATIONS.items():
        for isin in entry.get("isins", ()):
            key = isin.strip().upper()
            if key in index:
                raise ValueError(
                    f"ISIN {key} is claimed by both {index[key]} and {symbol} in "
                    f"ETF_ALLOCATIONS; one fund would silently inherit the other's basket"
                )
            index[key] = symbol
    return index


_ISIN_TO_SYMBOL = _build_isin_index()


def fund_isins() -> frozenset:
    """Every ISIN this table knows to be a fund."""
    return frozenset(_ISIN_TO_SYMBOL)


def is_known_etf_isin(isin: str) -> bool:
    """
    Is this ISIN a fund we hold a look-through opinion about?

    THE fundness predicate. Keyed on ISIN rather than symbol, so the UCITS and US lines
    of one ticker are distinguishable, and case-insensitive because issuer files disagree
    about ISIN casing — a lower-cased ISIN failing to match would silently reclassify a
    fund as a company.
    """
    return bool(isin) and isin.strip().upper() in _ISIN_TO_SYMBOL


def symbol_for_fund_isin(isin: str):
    """The mapped symbol for a fund ISIN, or None. Used for labelling, never for logic."""
    if not isin:
        return None
    return _ISIN_TO_SYMBOL.get(isin.strip().upper())


def allocation_for_fund_isin(isin: str):
    """
    A held security's allocation row, resolved from its **ISIN**, or None.

    The accessor `allocation_service` should use, and the reason it is one function rather
    than a composition at the call site: the two-step form
    `get_etf_allocation(symbol_for_fund_isin(isin))` is what a caller writes when it means
    this, and it is one missing None-check away from a `TypeError` and one habit away from
    reverting to the symbol lookup. Three call sites decided this question by symbol until
    2026-08-17, which made "is this a fund?" answerable two ways in one app — the ISIN way
    on the Look-through tab and the ticker way on the Allocation tab.
    """
    symbol = symbol_for_fund_isin(isin)
    return ETF_ALLOCATIONS.get(symbol) if symbol else None
