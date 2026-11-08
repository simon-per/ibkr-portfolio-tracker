"""
Guards on the fund registry (`app/etf_mappings.py`).

**What it is for now.** Since 2026-11-08 the registry only says which ISINs are funds; a fund's
sectors and regions come from its stored basket through the look-through's decomposition
(docs/lookthrough.md, *Allocation is a rollup of this decomposition*). So the failure worth
pinning is the one that stays: **a missing fund is worse than a wrong one.** An unregistered fund
falls through to `security.asset_type` (column default `"Stock"`) and is decomposed as one
company — drawn as a single business on the Look-through and in the Allocation charts, at a
plausible weight. That is how SOXQ sat after 2026-07-27 and IQQ after 2026-08-21.

Offline: pure data, no network.
"""
from app.etf_mappings import ETF_ALLOCATIONS, get_etf_allocation, is_known_etf


def test_no_entry_carries_an_allocation_estimate():
    """
    The hand-written `sector` and `geographic` blocks were retired on 2026-11-08 for the measured
    baskets. A block added back would be read by nothing — or worse, by a new reader, which would
    make it the second answer to a question the decomposition already answers.
    """
    extra = {
        symbol: sorted(set(entry) - {"isins", "asset_type"})
        for symbol, entry in ETF_ALLOCATIONS.items()
        if set(entry) - {"isins", "asset_type"}
    }
    assert extra == {}, (
        f"{extra}: the registry says which ISINs are funds; a fund's sectors and regions come "
        f"from its basket (LookthroughService.decompose)"
    )


def test_every_entry_is_marked_an_etf():
    assert {entry["asset_type"] for entry in ETF_ALLOCATIONS.values()} == {"ETF"}


def test_the_semiconductor_funds_held_here_are_both_registered():
    # SOXQ was bought 2026-07-27 and went unregistered, the failure this file's docstring
    # describes. SMH was already present.
    assert is_known_etf("SOXQ")
    assert is_known_etf("SMH")


def test_the_three_funds_bought_on_2026_08_06_are_registered():
    """The same failure as SOXQ, caught before it lands rather than after."""
    for symbol in ("VT", "GRID", "QTUM"):
        assert is_known_etf(symbol), f"{symbol} was bought and never registered"


def test_the_funds_bought_in_the_august_rotation_are_registered():
    """
    The rotation on 2026-08-21 sold the Ireland-domiciled sleeve and bought US-domiciled
    replacements. IQQ sat unregistered until 2026-08-24, drawn as one company.
    """
    for symbol in ("IQQ", "QQQM"):
        entry = get_etf_allocation(symbol)
        assert entry is not None, f"{symbol} was bought and is not registered"
        assert entry["asset_type"] == "ETF"


def test_an_unregistered_symbol_reports_itself_rather_than_guessing():
    # The caller branches on `is_known_etf`, so the None is the contract.
    assert not is_known_etf("NOT_AN_ETF")
    assert get_etf_allocation("NOT_AN_ETF") is None
