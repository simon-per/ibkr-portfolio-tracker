"""
Guards on `app/etf_sources.py` and the fund-identity half of `app/etf_mappings.py`.

Two tables now describe the funds this account holds — what they are made of
(`ETF_ALLOCATIONS`) and where a real basket comes from (`FUND_SOURCES`) — and CLAUDE.md's
opening warning is that duplicated knowledge diverges quietly. So the relationship is
asserted rather than trusted: neither table may know about a fund the other does not.

The `portfolio_id` uniqueness check is the sharpest one here. Several share classes of one
iShares fund are separate product ids over the same portfolio and return byte-identical
baskets, so a copy-pasted id is *invisible* — the fetch succeeds, the row counts look sane,
and one fund silently carries another's holdings.

Offline: pure data, no network.
"""
import pytest

from app.etf_mappings import (
    ETF_ALLOCATIONS,
    _build_isin_index,
    fund_isins,
    is_known_etf,
    is_known_etf_isin,
    symbol_for_fund_isin,
)
from app.etf_sources import ADAPTERS, FUND_SOURCES, ISSUER_OVERRIDES, source_for_fund_isin


def test_the_registry_is_not_vacuously_empty():
    """
    A table that quietly stopped being populated would make every assertion below pass on
    nothing, which is worse than no test — the same guard `test_flex_attr_coverage` keeps.
    """
    assert len(FUND_SOURCES) >= 12, f"only {len(FUND_SOURCES)} funds declared"
    assert len(fund_isins()) >= 12, f"only {len(fund_isins())} fund ISINs mapped"


def test_neither_fund_table_knows_a_fund_the_other_does_not():
    """
    `FUND_SOURCES` must never become a second census of what counts as a fund — that
    predicate has already diverged once in this codebase, between `securities.asset_type`
    and `ETF_ALLOCATIONS`.
    """
    assert set(FUND_SOURCES) - fund_isins() == set(), (
        "these ISINs declare a holdings source but are not in ETF_ALLOCATIONS, so "
        "`is_known_etf_isin` would treat the holding as an ordinary company"
    )
    assert fund_isins() - set(FUND_SOURCES) == set(), (
        "these mapped funds have no declared source, so the look-through view cannot say "
        "why their constituents are missing"
    )


@pytest.mark.parametrize("isin", sorted(FUND_SOURCES))
def test_every_fund_either_has_an_adapter_or_says_why_it_has_none(isin):
    """
    'No basket' must always be explainable on screen. An entry with neither an adapter nor a
    reason produces a fund the UI can only report as mysteriously absent.
    """
    source = FUND_SOURCES[isin]
    if source.adapter is None:
        assert source.exclude_reason.strip(), (
            f"{source.symbol} has no adapter and no exclude_reason"
        )
    else:
        assert source.adapter in ADAPTERS, (
            f"{source.symbol} names adapter {source.adapter!r}, which no parser implements"
        )


@pytest.mark.parametrize("isin", sorted(FUND_SOURCES))
def test_every_entry_carries_the_symbol_and_name_the_ui_labels_it_with(isin):
    source = FUND_SOURCES[isin]
    assert source.symbol.strip()
    assert source.name.strip()
    assert len(isin) == 12, f"{isin!r} is not an ISIN"
    assert source.leverage >= 1.0
    assert source.replication in ("physical", "synthetic")


def test_no_two_funds_share_a_blackrock_portfolio_id():
    """
    The invisible copy-paste slip: share classes of one fund are distinct product ids over
    the same portfolio, so a duplicated id fetches successfully and silently gives one fund
    another's basket. Nothing downstream could detect it.
    """
    seen = {}
    for isin, source in sorted(FUND_SOURCES.items()):
        pid = source.params.get("portfolio_id")
        if not pid:
            continue
        assert pid not in seen, (
            f"{source.symbol} and {seen[pid]} both claim portfolio_id={pid}; one would "
            f"silently carry the other's constituents"
        )
        seen[pid] = source.symbol


def test_no_two_funds_share_a_vanguard_ticker():
    """Same failure, other adapter."""
    seen = {}
    for isin, source in sorted(FUND_SOURCES.items()):
        ticker = source.params.get("ticker")
        if not ticker:
            continue
        assert ticker not in seen, (
            f"{source.symbol} and {seen[ticker]} both claim ticker={ticker!r}"
        )
        seen[ticker] = source.symbol


def test_the_synthetic_leveraged_fund_is_excluded_for_both_of_its_reasons():
    """
    DBPG is disqualified twice: its published basket is substitute collateral rather than
    the index, AND it is 2x leveraged. The two are recorded separately so a future "we found
    the swap reference basket" change cannot clear the first and silently reintroduce a 50%
    understatement from the second.
    """
    dbpg = source_for_fund_isin("LU0411078552")
    assert dbpg is not None and dbpg.symbol == "DBPG"
    assert dbpg.replication == "synthetic"
    assert dbpg.leverage == 2.0
    assert not dbpg.look_through_eligible
    reason = dbpg.exclude_reason.lower()
    assert "collateral" in reason, "the reason must say the basket is not the index"
    assert "leverage" in reason or "2x" in reason, "the reason must also name the leverage"


@pytest.mark.parametrize("isin", sorted(ISSUER_OVERRIDES))
def test_every_issuer_override_carries_its_evidence(isin):
    """
    This is the one tier with no external evidence behind it, so the reason is mandatory —
    the discipline `MINOR_UNIT_CURRENCIES` and `TICKER_CURRENCY_OVERRIDES` follow.
    """
    override = ISSUER_OVERRIDES[isin]
    assert len(isin) == 12, f"{isin!r} is not an ISIN"
    assert len(override.same_company_as_isin) == 12
    assert override.same_company_as_isin != isin, "an override to itself does nothing"
    assert len(override.reason.strip()) > 40, (
        f"{isin}'s override reason is too short to explain why no identifier folds it"
    )


def test_the_override_table_stays_small():
    """
    Growth here means a tier below is being misused rather than that the world got more
    complicated — the failure mode is a hand-maintained list quietly becoming the primary
    mechanism. Raise this deliberately, with a note, rather than by reflex.
    """
    assert len(ISSUER_OVERRIDES) <= 10, (
        f"{len(ISSUER_OVERRIDES)} issuer overrides. If real, raise the bound and say why; "
        f"if not, check whether GLEIF or OpenFIGI now answers for these ISINs."
    )


def test_the_isin_predicate_distinguishes_the_two_smh_funds():
    """
    The live collision the ISIN keying exists for: this account holds the UCITS VanEck fund
    (IE00BMC38736, LSE), not the far better-known US-listed SMH. A symbol-keyed lookup
    cannot tell them apart, and they are different instruments.
    """
    assert is_known_etf_isin("IE00BMC38736")
    assert symbol_for_fund_isin("IE00BMC38736") == "SMH"
    # The symbol form still answers, because allocation_service merges by symbol.
    assert is_known_etf("SMH")


def test_the_isin_predicate_ignores_case_and_whitespace():
    """Issuer files disagree about ISIN casing; a miss here reclassifies a fund as a company."""
    assert is_known_etf_isin("  ie00bmc38736 ")
    assert symbol_for_fund_isin("ie00bmc38736") == "SMH"


def test_an_unknown_isin_reports_itself_rather_than_guessing():
    assert not is_known_etf_isin("US0000000000")
    assert not is_known_etf_isin("")
    assert symbol_for_fund_isin("US0000000000") is None
    assert source_for_fund_isin("US0000000000") is None


def test_two_funds_claiming_one_isin_fails_at_import_not_on_a_request(monkeypatch):
    """
    The index is derived rather than maintained beside the table, and a duplicate ISIN is a
    copy-paste slip that would otherwise give one fund the other's basket. It must raise
    where a human sees it — at import — rather than surfacing as a wrong number later.
    """
    monkeypatch.setattr(
        "app.etf_mappings.ETF_ALLOCATIONS",
        {
            "AAA": {"isins": ["IE00B4L5Y983"], "asset_type": "ETF",
                    "sector": {}, "geographic": {}},
            "BBB": {"isins": ["IE00B4L5Y983"], "asset_type": "ETF",
                    "sector": {}, "geographic": {}},
        },
    )
    with pytest.raises(ValueError, match="claimed by both"):
        _build_isin_index()


@pytest.mark.parametrize("symbol", sorted(ETF_ALLOCATIONS))
def test_every_mapped_fund_declares_at_least_one_isin(symbol):
    """
    Without an ISIN an entry is invisible to `is_known_etf_isin`, so the holding would be
    treated as an ordinary company and distributed into the company table as though a fund
    were a business.
    """
    isins = ETF_ALLOCATIONS[symbol].get("isins")
    assert isins, f"{symbol} declares no ISINs"
    for isin in isins:
        assert len(isin) == 12 and isin == isin.strip().upper(), (
            f"{symbol} declares {isin!r}, which is not a normalised ISIN"
        )
