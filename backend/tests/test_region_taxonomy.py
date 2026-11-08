"""
Guards on `app/services/region_taxonomy.py` — the one map from a country to a region.

The Regions chart is a rollup of every company's region (docs/lookthrough.md, *Allocation is a
rollup of this decomposition*), so a country this module cannot place becomes value in `Unknown`
rather than an error. These pin that it places every country, reads the spellings the providers
actually write, and keeps an ISIN's registration prefix in its place: a fallback that refuses
offshore incorporations rather than guessing.

Offline: pure functions, plus one read of the frontend's mirror of the region list.
"""
import re
from pathlib import Path

import pytest

from app.services import region_taxonomy as rt
from app.services.region_taxonomy import (
    REGIONS,
    UNKNOWN,
    country_code,
    region_of,
    region_of_isin,
    unmapped_countries,
)

SECTOR_COLORS_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "sectorColors.ts"


def test_every_country_is_placed_in_one_of_the_regions():
    misplaced = {code: row[2] for code, row in rt._COUNTRIES.items() if row[2] not in REGIONS}
    assert misplaced == {}


def test_every_country_answers_to_its_code_its_alpha3_and_its_name():
    for code, (alpha3, name, region) in rt._COUNTRIES.items():
        # Namibia's alpha-2 is the one deliberate exception: "NA" is what providers write for
        # "none", so it is read as that (`test_a_non_country_is_unknown_and_never_raises`).
        spellings = (alpha3, name, name.upper()) if code == "NA" else (code, alpha3, name, name.upper())
        for spelling in spellings:
            assert region_of(spelling) == region, f"{spelling!r} -> {region_of(spelling)!r}"


def test_no_alias_points_at_a_country_that_does_not_exist():
    assert {a: c for a, c in rt._ALIASES.items() if c not in rt._COUNTRIES} == {}


@pytest.mark.parametrize("spelling, region", [
    # What the adapters and Yahoo write for the countries this book holds.
    ("United States", "North America"),
    ("United States of America", "North America"),
    ("USA", "North America"),
    ("Korea (South)", "Asia Pacific"),          # iShares
    ("Korea, Republic of", "Asia Pacific"),     # ISO long form
    ("South Korea", "Asia Pacific"),            # Yahoo
    ("Taiwan, Province of China", "Asia Pacific"),
    ("Hong Kong SAR, China", "Asia Pacific"),
    ("Netherlands", "Europe"),
    ("The Netherlands", "Europe"),
    ("Switzerland", "Europe"),
    ("United Kingdom", "Europe"),
    ("Türkiye", "Europe"),                      # an override of M49's Western Asia
    ("Cyprus", "Europe"),                       # likewise
    ("Iran", "Middle East & Africa"),           # an override of M49's Southern Asia
    ("Israel", "Middle East & Africa"),
    ("Saudi Arabia", "Middle East & Africa"),
    ("South Africa", "Middle East & Africa"),
    ("Brazil", "Latin America"),
    ("Mexico", "Latin America"),
    ("Puerto Rico", "North America"),           # a US territory, not the Caribbean
    ("Bermuda", "North America"),
    ("Japan", "Asia Pacific"),
    ("India", "Asia Pacific"),
    ("Australia", "Asia Pacific"),
    ("Europe", "Europe"),                       # a region named outright
])
def test_the_spellings_providers_write(spelling, region):
    assert region_of(spelling) == region


@pytest.mark.parametrize("spelling", [
    None, "", "-", "n/a", "NA", "Unknown", "Other", "Cash", "Global", "Emerging Markets",
    "Atlantis",
])
def test_a_non_country_is_unknown_and_never_raises(spelling):
    """`NA` is the case that matters: it is Namibia's code, and providers write it for "none"."""
    assert region_of(spelling) == UNKNOWN


def test_country_code_reads_the_same_spellings():
    assert country_code("Korea (South)") == "KR"
    assert country_code("U.S.") == "US"
    assert country_code("n/a") is None
    assert country_code(None) is None


@pytest.mark.parametrize("isin, region", [
    ("US0378331005", "North America"),   # Apple
    ("NL0010273215", "Europe"),          # ASML
    ("TW0002330008", "Asia Pacific"),    # TSMC
    ("KR7005930003", "Asia Pacific"),    # Samsung
    ("BRVALEACNOR0", "Latin America"),
    ("KYG017191142", UNKNOWN),           # Cayman: says where it was incorporated, not where it is
    ("BMG0450A1053", UNKNOWN),           # Bermuda, likewise
    ("JE00B4T3BW64", UNKNOWN),           # Jersey
    ("XS1234567890", UNKNOWN),           # Euroclear: not a country at all
    ("", UNKNOWN),
    (None, UNKNOWN),
])
def test_an_isins_prefix_is_a_fallback_that_refuses_offshore(isin, region):
    assert region_of_isin(isin) == region


def test_unmapped_spellings_are_reported_and_declared_unknowns_are_not():
    assert unmapped_countries(["United States", "n/a", None, "Europe", "Atlantis"]) == {"Atlantis"}


@pytest.mark.skipif(not SECTOR_COLORS_TS.exists(), reason="frontend/ not present")
def test_the_frontends_region_order_mirrors_this_module():
    """
    `REGION_ORDER` in lib/sectorColors.ts gives each region its palette slot, so a region the
    server publishes and the client does not list would be painted as `Other`.
    """
    source = SECTOR_COLORS_TS.read_text(encoding="utf-8")
    block = re.search(r"export const REGION_ORDER = \[(.*?)\] as const", source, re.S)
    assert block, "REGION_ORDER not found in sectorColors.ts"
    assert tuple(re.findall(r"'([^']+)'", block.group(1))) == REGIONS
