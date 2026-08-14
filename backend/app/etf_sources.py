"""
Where each held fund's constituent basket comes from, and the hand-declared company
identities no identifier can establish.

Hand-maintained and reviewed in git, exactly like `EXCHANGE_SUFFIXES` and
`MINOR_UNIT_CURRENCIES`, because every entry is a human decision: which endpoint speaks
for a fund, and whether a fund can be looked through at all. The alternative — editing it
over ssh — is the reason `manage_mappings.py` exists.

**This is NOT a second census of what counts as a fund.** `app/etf_mappings.py` owns that
(`is_known_etf_isin`), and `tests/test_etf_source_registry.py` asserts every ISIN here is
reachable from `ETF_ALLOCATIONS`. A third answer to "is this a fund?" would be
CLAUDE.md's dominant failure mode for the third time — that predicate already diverged
once between `securities.asset_type` and the mapping table.

**The read path never touches `adapter`.** It reads `etf_baskets` / `etf_holdings` and, if
a fund has no stored basket, reports it as uncovered whatever this file claims. So an
adapter named here before it is implemented degrades to "no basket yet", never to a wrong
number.

Network etiquette, since these are third-party sites rather than an API we are a customer
of: a descriptive User-Agent carrying a contact address, one request at a time, no retry
storm, and nothing in this app exports raw issuer weights. The data is fine to cache
privately for a single portfolio and is not ours to redistribute.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional

from app.config import settings
from app.services.company_identity import IssuerOverride

# Adapter names that `app/services/etf_basket_parsers.py` knows how to parse. `manual`
# means the only route in is a file downloaded by hand and ingested through
# `app/cli/import_etf_basket.py` — a real answer, not a placeholder: for VWCE it is the
# permanent one, because Vanguard Europe's GraphQL endpoint requires an undocumented
# header, and it publishes only its top ten holdings — complete holdings are request-only.
ADAPTERS = frozenset({"blackrock", "dws", "vanguard_us", "manual"})


@dataclass(frozen=True)
class FundSource:
    """How to obtain one fund's basket, and whether its basket means anything."""
    symbol: str
    name: str
    adapter: Optional[str] = None
    params: Dict[str, str] = field(default_factory=dict)

    # 'physical' | 'synthetic'. Recorded SEPARATELY from `leverage` on purpose: DBPG is
    # disqualified twice over, and folding the two into one flag would let a later "we
    # found the swap reference basket" change clear the synthetic objection and silently
    # reintroduce a 50% understatement from the leverage it forgot about.
    replication: str = "physical"
    leverage: float = 1.0

    # Non-empty means: do not look this fund through, and say this instead. Surfaced to
    # the API verbatim, so the explanation lives where it is read.
    exclude_reason: str = ""

    @property
    def look_through_eligible(self) -> bool:
        return not self.exclude_reason


# Keyed by fund ISIN. Every value verified against this account's own `securities` rows
# on 2026-08-14 — a web lookup got four of these wrong (GRID, SOXQ, QTUM and SMH, the
# last by returning the US VanEck fund rather than the UCITS line actually held).
FUND_SOURCES: Dict[str, FundSource] = {
    # --- iShares / BlackRock -------------------------------------------------------
    # `portfolio_id` is not derivable from the ISIN and was discovered from
    # https://www.ishares.com/uk/product-sitemap.xml, then confirmed by fetching the
    # basket and matching its row count. Note that several share classes of one fund
    # (accumulating, distributing, hedged) are separate product ids over the SAME
    # portfolio and return byte-identical baskets — 287737 / 309035 / 332655 all give
    # MSCI World's 1338 rows — so the lowest id is recorded for determinism rather than
    # because the others are wrong.
    "IE00B4L5Y983": FundSource(
        symbol="IWDA",
        name="iShares Core MSCI World UCITS ETF",
        adapter="blackrock",
        params={"portfolio_id": "287737"},
    ),
    "IE00B5BMR087": FundSource(
        symbol="SXR8",
        name="iShares Core S&P 500 UCITS ETF",
        adapter="blackrock",
        params={"portfolio_id": "286083"},
    ),
    "IE00BKM4GZ66": FundSource(
        symbol="EMIM",
        name="iShares Core MSCI EM IMI UCITS ETF",
        adapter="blackrock",
        params={"portfolio_id": "264659"},
    ),

    # --- Xtrackers / DWS -----------------------------------------------------------
    # Keyed by the fund's own ISIN, so there is nothing to discover and nothing to keep
    # in step. The export echoes `ShareClass ISIN` on every row, which the parser checks
    # against the ISIN it asked for — the cheapest possible guard against a redirect
    # serving another fund's basket.
    "IE00BMFKG444": FundSource(
        symbol="XNAS",
        name="Xtrackers NASDAQ 100 UCITS ETF",
        adapter="dws",
    ),
    "IE00BGV5VN51": FundSource(
        symbol="XAIX",
        name="Xtrackers Artificial Intelligence & Big Data UCITS ETF",
        adapter="dws",
    ),

    # --- Vanguard (US listings only) -----------------------------------------------
    # The US profile API is keyed by lower-cased ticker and pages 500 rows at a time, so
    # VT's ~10,000 holdings take 21 requests. Its as-of is a month end and lags ~6 weeks,
    # which is normal for Vanguard and must not read as staleness.
    "US9220427424": FundSource(
        symbol="VT",
        name="Vanguard Total World Stock ETF",
        adapter="vanguard_us",
        params={"ticker": "vt"},
    ),

    # --- No programmatic route: import a downloaded file ---------------------------
    # The largest single position in the account, so its basket matters more than any other —
    # and there is genuinely nothing to fetch. **The blocker is that the data is not published,
    # NOT that we are forbidden to read it**, and getting that backwards (as an earlier revision
    # of this comment did) would stop the next person looking for the route that does exist:
    # every Vanguard EU domain's robots.txt is `User-agent: * / Disallow:` — allow-all — and only
    # model-training crawlers are named. What is actually missing: the product page publishes the
    # top ten holdings with no ISIN column, the US profile API that serves VT rejects the Irish
    # fund id, live EU holdings are GraphQL-only behind an undocumented `x-consumer-id`, and
    # there is no EU analogue to SEC N-PORT. Vanguard Funds plc's own disclosure policy makes
    # complete holdings request-only (month-end + 15 days, by email). The semiannual report PDF
    # does carry every holding but no ISINs at all, and our pipeline is ISIN-keyed.
    "IE00BK5BQT80": FundSource(
        symbol="VWCE",
        name="Vanguard FTSE All-World UCITS ETF",
        adapter="manual",
    ),
    # **A route exists and is worth building** — this entry is `manual` only because the adapter
    # has not been written yet, not because nothing is fetchable. Verified 2026-08-14:
    # `https://www.vaneck.com/nl/en/investments/semiconductor-etf/downloads/holdings/` returns a
    # ~5 kB XLSX carrying `Holding Name | Ticker | ISIN | Shares | Market Value | % of Net
    # Assets`, **as-of T-1** — fresher than every feed here except the Xtrackers CSV, and the
    # smallest unit of work of any remaining gap. Two things it needs: a **cookie jar** (a
    # cookieless GET loops the geo/consent redirects indefinitely) and the `/nl/en/` locale
    # **pinned** rather than trusting `/ucits/` to geo-resolve the same way twice. Parse the zip
    # with `zipfile` + `ElementTree` rather than adding an `openpyxl` dependency.
    # Do NOT substitute the US SMH's SEC filing: different index variant, different membership.
    "IE00BMC38736": FundSource(
        symbol="SMH",
        name="VanEck Semiconductor UCITS ETF",
        adapter="manual",
    ),
    # Invesco serves a single-page app; the documented holdings URL returns the shell.
    "US46138G6153": FundSource(
        symbol="SOXQ",
        name="Invesco PHLX Semiconductor ETF",
        adapter="manual",
    ),
    # First Trust renders a server-side HTML table with tickers but no ISINs, so even a
    # successful scrape could not be folded against a direct holding reliably.
    "US33737A1088": FundSource(
        symbol="GRID",
        name="First Trust Nasdaq Clean Edge Smart Grid Infrastructure Index Fund",
        adapter="manual",
    ),
    # Defiance publishes a WordPress table carrying CUSIPs but no ISINs.
    "US26922A4206": FundSource(
        symbol="QTUM",
        name="Defiance Quantum ETF",
        adapter="manual",
    ),

    # --- Excluded ------------------------------------------------------------------
    "LU0411078552": FundSource(
        symbol="DBPG",
        name="Xtrackers S&P 500 2x Leveraged Daily Swap UCITS ETF",
        adapter=None,
        replication="synthetic",
        leverage=2.0,
        exclude_reason=(
            "Synthetic swap-based fund: the basket it publishes is substitute collateral, "
            "not the index it tracks (measured top holdings Mastercard 6.6%, Altria 5.7%, "
            "Tesla 4.9% for an S&P 500 product). It is also 2x leveraged, so even the real "
            "index basket would understate its exposure by half."
        ),
    ),
}


# Hand-declared statements that two ISINs are the same company, for the cases no
# identifier can establish. Deliberately tiny: `tests/test_etf_source_registry.py`
# requires a non-empty reason on every entry, and growth here means a tier below is
# being misused rather than that the world got more complicated.
ISSUER_OVERRIDES: Dict[str, IssuerOverride] = {
    "US8740391003": IssuerOverride(
        same_company_as_isin="TW0002330008",
        reason=(
            "TSMC's ADR and its Taiwanese ordinary are one company and no identifier "
            "joins them (measured 2026-08-14): shareClassFIGI differs "
            "(BBG001S5WWW4 vs BBG001S6Q004, as it must — an ADR is a distinct "
            "instrument), and GLEIF gives the ADR a LEI (549300KB6NK5SBD14S87) while "
            "having no record at all for the ordinary. The ordinary is what this account "
            "holds directly (2330@TWSE) and the ADR is what the US-listed funds hold, so "
            "without this TSMC would show as two rows."
        ),
    ),
}


def source_for_fund_isin(isin: str) -> Optional[FundSource]:
    """The declared source for a fund ISIN, or None if we have never declared one."""
    if not isin:
        return None
    return FUND_SOURCES.get(isin.strip().upper())


def user_agent() -> str:
    """
    How this app identifies itself to OpenFIGI, GLEIF and the issuer sites.

    Lives here rather than beside either caller because it is part of the etiquette this
    module documents, and because both the identity resolver and the basket fetcher must send
    the *same* thing — two user agents would make one of them unattributable in somebody
    else's logs. The contact address comes from settings, since this repository is public and
    an operator's email does not belong in it.
    """
    contact = (settings.lookthrough_contact_email or "").strip()
    suffix = f" (+{contact})" if contact else ""
    return f"ibkr-portfolio-tracker/{settings.app_version}{suffix}"
