"""
The sector normaliser, and the four vocabularies it has to speak.

Every raw string asserted here was **read off this account's own data on 2026-08-16**, not invented:
`securities.sector` holds Yahoo's names, `etf_holdings.sector` holds iShares' GICS-ish names for
5,890 rows and Xtrackers' own for 89, and Vanguard's 10,032 rows hold nothing at all. The point of
pinning the observed strings rather than a tidy sample is that a provider renaming one is exactly
the change that would otherwise show up only as a quietly growing Unknown bucket.
"""
import pytest

from app.services.sector_taxonomy import (
    CANONICAL_SECTORS,
    UNKNOWN,
    is_canonical,
    normalise,
    unmapped_sectors,
)

# Distinct values measured in production, per source.
YAHOO_OBSERVED = [
    "Basic Materials", "Communication Services", "Consumer Cyclical",
    "Financial Services", "Technology",
]
ISHARES_OBSERVED = [
    "Cash and/or Derivatives", "Communication", "Consumer Discretionary",
    "Consumer Staples", "Energy", "Financials", "Health Care", "Industrials",
    "Information Technology", "Materials", "Other", "Real Estate", "Utilities",
]
XTRACKERS_OBSERVED = [
    "Consumer Discretionary", "Financials", "Industrials", "Technology",
    "Telecommunications",
]


@pytest.mark.parametrize("raw", YAHOO_OBSERVED + XTRACKERS_OBSERVED)
def test_every_observed_yahoo_and_xtrackers_value_maps_to_a_canonical_sector(raw):
    assert is_canonical(normalise(raw)), f"{raw!r} fell through to {normalise(raw)!r}"


@pytest.mark.parametrize("raw", [v for v in ISHARES_OBSERVED
                                 if v not in ("Cash and/or Derivatives", "Other")])
def test_every_observed_ishares_value_maps_to_a_canonical_sector(raw):
    assert is_canonical(normalise(raw)), f"{raw!r} fell through to {normalise(raw)!r}"


def test_the_taxonomies_agree_after_normalising():
    """
    The whole reason this module exists: the same company is classified differently by each
    provider, and the differences are purely lexical. Measured pairs, from production.
    """
    for yahoo, ishares in [
        ("Technology", "Information Technology"),
        ("Financial Services", "Financials"),
        ("Communication Services", "Communication"),
        ("Consumer Cyclical", "Consumer Discretionary"),
    ]:
        assert normalise(yahoo) == normalise(ishares), (
            f"{yahoo!r} and {ishares!r} describe the same sector and must fold together"
        )


def test_ishares_communication_has_no_services_suffix_and_is_still_folded():
    """
    A regression guard with a name, because this one is easy to "correct" away. iShares writes
    `Communication`, not GICS's `Communication Services` — verified against the stored baskets —
    and dropping the alias would give Alphabet and Meta their own group at 15% of this book.
    """
    assert normalise("Communication") == "Communications"


@pytest.mark.parametrize("raw", ["Other", "Cash and/or Derivatives", "unknown", "Unknown",
                                 "unclassified", "N/A", "-", "", "   "])
def test_a_value_carrying_no_classification_becomes_unknown(raw):
    assert normalise(raw) == UNKNOWN


def test_none_becomes_unknown_rather_than_raising():
    """
    Vanguard sets no sector on any of its 10,032 rows, and `securities.sector` is NULL for any
    security nobody has run the allocation sync against. Both arrive here as None, constantly.
    """
    assert normalise(None) == UNKNOWN


def test_case_and_padding_do_not_create_new_buckets():
    for raw in ["technology", "  TECHNOLOGY  ", "Technology", "TeChNoLoGy", "Health  Care"]:
        assert is_canonical(normalise(raw))
    assert normalise("Health  Care") == "Healthcare"   # collapsed internal whitespace


def test_an_unmapped_spelling_becomes_unknown_instead_of_its_own_group():
    """
    A provider's private vocabulary must never reach the chart. One unmapped string would render
    as a one-company group labelled with it, which reads as a real sector rather than as a gap.
    """
    assert normalise("Electronic Equipment: Control and Filter") == UNKNOWN


def test_unmapped_sectors_reports_what_needs_an_alias():
    """
    The diagnostic that tells a growing Unknown bucket apart from a missing alias. `Other` is
    declared-unknown and must not be reported as needing one.
    """
    found = unmapped_sectors([
        "Technology", "Other", None, "Diversified Industrials", "Health Care",
    ])
    assert found == {"Diversified Industrials"}


def test_the_canonical_order_is_fixed():
    """
    `sectorColors.ts` assigns hues by position in this tuple, so a sector keeps its colour
    whatever the portfolio holds this week — the "colour follows the entity, never its rank"
    rule. Reordering it silently recolours the chart.
    """
    assert CANONICAL_SECTORS[0] == "Technology"
    assert CANONICAL_SECTORS[1] == "Communications"
    assert len(CANONICAL_SECTORS) == len(set(CANONICAL_SECTORS)) == 11
    assert UNKNOWN not in CANONICAL_SECTORS
