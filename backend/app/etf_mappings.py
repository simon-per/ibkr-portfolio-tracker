"""
The fund registry: which securities are funds, keyed by ISIN.

**It no longer carries allocation percentages.** Until 2026-11-08 every entry held hand-written
`sector` and `geographic` blocks, and the Allocation tab spread each fund's value by them. They
were estimates — sectors copied from Yahoo's `funds_data`, regions derived from top-ten holdings
covering as little as 15% of a fund — and they were a second answer beside the stored baskets the
Look-through measured the same funds from. This docstring used to list the two preconditions for
retiring them, and both are met: every held fund has a stored basket (the two 3a funds through
their declared, accepted proxies), and `app/services/region_taxonomy.py` is the one
country-to-region map. The Allocation tab is now a rollup of `LookthroughService.decompose`
(docs/lookthrough.md, *Allocation is a rollup of this decomposition*); a fund without a basket
lands in `Unknown` there, under its own name, rather than being spread by a guess. The name
`ETF_ALLOCATIONS` survives because every fundness predicate below reads it.

**`isins` per entry exists because a symbol is not an identity, and this table was
already wrong about one fund.** `securities` identity is `isin + exchange`, symbols are
not unique, and `SMH` is the live collision: this account held the **UCITS** VanEck
Semiconductor fund (`IE00BMC38736`, LSE, USD) while the far better-known `SMH` is the
US-listed VanEck fund — a symbol-keyed lookup cannot tell them apart, and the two are
different instruments with different domiciles. So every entry declares the ISINs it
covers, and `fund_isins()` / `is_known_etf_isin()` / `symbol_for_fund_isin()` below are
**the** fundness predicates. CLAUDE.md records that this predicate has already diverged
once (`securities.asset_type` said "Stock" while this table said "ETF"); a third answer
would be the same bug a third time, so nothing outside this module may decide it.
`tests/test_etf_source_registry.py::test_every_declared_source_is_a_known_fund` holds the
line the one place it could realistically be crossed, by requiring `FUND_SOURCES` and
`ETF_ALLOCATIONS` to name the same ISINs in **both** directions.

**A fund missing from here is not loudly broken.** It takes the `securities.asset_type` column
default "Stock", so it is drawn as one *company* on the Look-through and in the Allocation
charts, at a plausible weight. SOXQ (2026-07-27) and IQQ (2026-08-21) each sat in that state
until added — add a fund here, and its holdings source to `etf_sources.py`, before or as it is
bought.
"""

ETF_ALLOCATIONS = {
    # iShares Core MSCI World. Also the basket donor for the Swisscanto World ex CH 3a tranche.
    "IWDA": {"isins": ["IE00B4L5Y983"], "asset_type": "ETF"},
    # Vanguard FTSE All-World; borrows VT's basket (etf_sources.py).
    "VWCE": {"isins": ["IE00BK5BQT80"], "asset_type": "ETF"},
    # iShares Core S&P 500.
    "SXR8": {"isins": ["IE00B5BMR087"], "asset_type": "ETF"},
    # Vanguard S&P 500 — not held; declared because it is DBPG's basket proxy, and
    # `test_etf_source_registry.py` requires the two tables to name the same ISINs.
    "VOO": {"isins": ["US9229083632"], "asset_type": "ETF"},
    # Xtrackers NASDAQ 100.
    "XNAS": {"isins": ["IE00BMFKG444"], "asset_type": "ETF"},
    # iShares Nasdaq 100 and Invesco NASDAQ 100 — the US-domiciled replacements bought in the
    # 2026-08 rotation out of the Ireland-domiciled sleeve.
    "IQQ": {"isins": ["US46438T3095"], "asset_type": "ETF"},
    "QQQM": {"isins": ["US46138G6492"], "asset_type": "ETF"},
    # Xtrackers S&P 500 2x Leveraged — synthetic; decomposed only through its proxy.
    "DBPG": {"isins": ["LU0411078552"], "asset_type": "ETF"},
    # VanEck Semiconductor UCITS ETF — Irish-domiciled, LSE-listed. NOT the US-listed SMH.
    "SMH": {"isins": ["IE00BMC38736"], "asset_type": "ETF"},
    # Invesco PHLX Semiconductor ETF.
    "SOXQ": {"isins": ["US46138G6153"], "asset_type": "ETF"},
    # Xtrackers Artificial Intelligence & Big Data.
    "XAIX": {"isins": ["IE00BGV5VN51"], "asset_type": "ETF"},
    # iShares Core MSCI Emerging Markets IMI.
    "EMIM": {"isins": ["IE00BKM4GZ66"], "asset_type": "ETF"},
    # iShares MSCI EM — not held; the basket donor for the Swisscanto EM 3a tranche.
    "IEEM": {"isins": ["IE00B0M63177"], "asset_type": "ETF"},
    # Vanguard Total World Stock.
    "VT": {"isins": ["US9220427424"], "asset_type": "ETF"},
    # First Trust NASDAQ Clean Edge Smart Grid Infrastructure.
    "GRID": {"isins": ["US33737A1088"], "asset_type": "ETF"},
    # Defiance Quantum.
    "QTUM": {"isins": ["US26922A4206"], "asset_type": "ETF"},

    # --- Swiss pillar 3a (finpension), Swisscanto institutional tranches -------------
    # Keyed by ISIN like everything else here; the "symbol" is the ISIN because
    # finpension publishes no ticker and `ticker_mappings` keys on (symbol, exchange),
    # so the symbol must never change.
    # Swisscanto Index Equity Fund Emerging Markets (IEEM's basket, etf_sources.py).
    "CH1529078078": {"isins": ["CH1529078078"], "asset_type": "ETF"},
    # Swisscanto IPF I Index Equity Fund World ex CH (IWDA's basket, etf_sources.py).
    "CH0117044948": {"isins": ["CH0117044948"], "asset_type": "ETF"},
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
    and would hand one fund the other's basket. Worse, a *stock* whose ticker collides
    with a row here would be decomposed as though it were a fund — a fabricated
    allocation rather than a caveat.

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
    A held security's registry row (`isins`, `asset_type`), resolved from its **ISIN**, or None.

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
