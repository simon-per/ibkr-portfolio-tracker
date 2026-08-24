"""
Guards on the hand-maintained ETF look-through table (`app/etf_mappings.py`).

`get_portfolio_allocation` distributes an ETF's market value across sectors and
regions using these percentages. Two things make the table worth pinning:

- **A row that does not sum to 100 silently distorts the treemap.** The weights
  are multiplied by the position's own weight, so a column summing to 90 quietly
  shrinks that ETF's contribution and the categories never add up. Nothing in the
  request path checks it, and the numbers are edited by hand.
- **A missing symbol is worse than a wrong one.** `is_known_etf` gates the whole
  look-through, so an unmapped ETF falls through to `security.asset_type` (column
  default `"Stock"`) and to `security.sector`/`security.country`, which Yahoo
  leaves empty for a fund. The holding then appears in *neither* the sector nor
  the geographic breakdown and is labelled a stock in the asset-type one — and no
  sync can repair it, because the missing data is a mapping rather than a fetch.
  That is how SOXQ sat invisible after 2026-07-27.

Offline: pure data, no network.
"""
import pytest

from app.etf_mappings import ETF_ALLOCATIONS, get_etf_allocation, is_known_etf


@pytest.mark.parametrize("symbol", sorted(ETF_ALLOCATIONS))
@pytest.mark.parametrize("dimension", ["sector", "geographic"])
def test_every_breakdown_sums_to_a_hundred(symbol, dimension):
    weights = ETF_ALLOCATIONS[symbol][dimension]
    total = sum(weights.values())
    assert total == pytest.approx(100.0, abs=0.01), (
        f"{symbol} {dimension} sums to {total}, so its share of the treemap is "
        f"scaled by {total / 100:.2f} rather than 1.0"
    )


@pytest.mark.parametrize("symbol", sorted(ETF_ALLOCATIONS))
def test_every_entry_declares_both_dimensions_and_is_marked_an_etf(symbol):
    entry = ETF_ALLOCATIONS[symbol]
    # A missing dimension raises KeyError inside the allocation loop rather than
    # degrading, so absence must fail here instead.
    assert entry["sector"], f"{symbol} has no sector breakdown"
    assert entry["geographic"], f"{symbol} has no geographic breakdown"
    assert entry["asset_type"] == "ETF"


@pytest.mark.parametrize("symbol", sorted(ETF_ALLOCATIONS))
def test_no_weight_is_negative_or_over_a_hundred(symbol):
    for dimension in ("sector", "geographic"):
        for name, pct in ETF_ALLOCATIONS[symbol][dimension].items():
            assert 0 < pct <= 100, f"{symbol} {dimension} {name} is {pct}"


def test_the_semiconductor_funds_held_here_are_both_mapped():
    # SOXQ was bought 2026-07-27 and went unmapped, which is the failure this
    # file's docstring describes. SMH was already present.
    assert is_known_etf("SOXQ")
    assert is_known_etf("SMH")
    # Both are pure-play semiconductor funds, so neither should have picked up a
    # non-technology sector by a copy-paste slip.
    for symbol in ("SOXQ", "SMH"):
        assert get_etf_allocation(symbol)["sector"] == {"Technology": 100.0}


def test_the_three_funds_bought_on_2026_08_06_are_mapped():
    """
    The same failure as SOXQ, caught before it lands rather than after.

    VT, GRID and QTUM were bought on 2026-08-06. `sync_helper` never writes
    `sector`/`country`, so an IBKR-ingested fund arrives with both NULL, and no sync can
    repair it — `allocation_service` falls through to Yahoo `.info`, which carries no
    sector or country for a fund. Without an entry here all three would land in the
    unattributed buckets on the Allocation tab.
    """
    for symbol in ("VT", "GRID", "QTUM"):
        assert is_known_etf(symbol), f"{symbol} was bought and never mapped"


def test_the_two_ftse_global_funds_do_not_drift_apart():
    """
    VT tracks FTSE Global All Cap and VWCE tracks FTSE All-World: one index family, whose
    regional weights differ only by small-cap inclusion. Their geographic blocks are
    pinned equal deliberately, so editing one alone fails here instead of quietly making
    one index look like two different portfolios depending on which wrapper is held.

    Their *sector* blocks are deliberately NOT compared — those are measured per ticker
    from Yahoo, so they may legitimately differ by a rounding or a rebalancing date.
    """
    assert (
        get_etf_allocation("VT")["geographic"]
        == get_etf_allocation("VWCE")["geographic"]
    ), (
        "VT and VWCE track the same FTSE global index family. If one gained better "
        "regional data, the other belongs in the same commit."
    )


def test_the_three_nasdaq_100_funds_do_not_drift_apart():
    """
    XNAS, IQQ and QQQM are three wrappers around one index, held across the 2026-08 rotation
    out of the Ireland-domiciled sleeve (XNAS sold, IQQ and QQQM bought). Both blocks are
    compared, not just geography: unlike VT/VWCE above there is no small-cap difference to
    excuse a divergence, so a different sector split here would mean the Allocation tab
    described the Nasdaq-100 differently depending on which wrapper the account happened to
    hold — three answers to one question, which is this codebase's oldest failure mode.
    """
    for dimension in ("geographic", "sector"):
        xnas = get_etf_allocation("XNAS")[dimension]
        for symbol in ("IQQ", "QQQM"):
            assert get_etf_allocation(symbol)[dimension] == xnas, (
                f"{symbol} and XNAS both track the Nasdaq-100, so their {dimension} blocks "
                f"must stay identical. If one gained better data, all three belong in the "
                f"same commit."
            )


def test_the_funds_bought_in_the_august_rotation_are_mapped():
    """
    The rotation on 2026-08-21 sold the Ireland-domiciled sleeve and bought US-domiciled
    replacements. An unmapped fund is not loudly broken — it takes the `securities.asset_type`
    column default of "Stock", so it is drawn as a *company* in the allocation charts and as a
    single company row in the look-through, at a plausible weight and with nothing reporting
    it. IQQ sat in exactly that state until 2026-08-24.
    """
    for symbol in ("IQQ", "QQQM"):
        allocation = get_etf_allocation(symbol)
        assert allocation is not None, f"{symbol} was bought and is not mapped"
        assert allocation["asset_type"] == "ETF"


def test_an_unmapped_symbol_reports_itself_rather_than_guessing():
    # The caller branches on `is_known_etf`, so the None is the contract.
    assert not is_known_etf("NOT_AN_ETF")
    assert get_etf_allocation("NOT_AN_ETF") is None
