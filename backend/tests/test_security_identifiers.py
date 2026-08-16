"""
CUSIP to ISIN, and the two ways it must refuse.

Every identifier here was read off a real issuer file or a real security on 2026-08-16 — the
whole value of a local conversion is that it is exact, so the fixtures have to be too.
"""
import pytest

from app.services.security_identifiers import (
    derive_north_american_isin,
    identifier_kind,
    is_north_american_cusip,
    looks_like_isin,
    looks_like_sedol,
)

# (CUSIP, the ISIN it must produce). Verified against the real securities.
KNOWN = [
    ("67066G104", "US67066G1040"),   # NVIDIA
    ("037833100", "US0378331005"),   # Apple
    ("02079K107", "US02079K1079"),   # Alphabet class C
    ("46138G615", "US46138G6153"),   # the SOXQ fund itself, from its own product page
    ("18915M107", "US18915M1071"),   # Cloudflare, from QTUM's basket
]

# Leading letters, so CINS rather than CUSIP. All measured in GRID's and QTUM's baskets.
CINS = [
    ("G29183103", "Eaton Corporation Plc"),
    ("F86921107", "Schneider Electric SE"),
    ("G51502105", "Johnson Controls International Plc"),
    ("N14506104", "Elastic Nv"),
    ("Y4000A102", "Horizon Quantum Holdings Ltd"),
    ("G0567U127", "Arqit Quantum Inc"),
]


@pytest.mark.parametrize("cusip,isin", KNOWN)
def test_a_north_american_cusip_converts_exactly(cusip, isin):
    assert derive_north_american_isin(cusip) == isin


@pytest.mark.parametrize("cusip,name", CINS)
def test_a_cins_is_refused_rather_than_prefixed_with_us(cusip, name):
    """
    The whole reason this module has a guard. `US` + a CINS produces a check-digit-valid ISIN
    that belongs to nothing, so the company would stand alone under a fabricated identifier
    instead of folding with its real listing. These go to OpenFIGI's ID_CUSIP mapping.
    """
    assert is_north_american_cusip(cusip) is False, f"{name} is a foreign issuer"
    assert derive_north_american_isin(cusip) is None


def test_the_us_and_canadian_forms_of_one_cusip_differ_and_only_one_is_real():
    """
    Documents the residual ambiguity rather than pretending it away: Canadian issuers share the
    digit-leading space, so the prefix cannot be inferred from the identifier alone. Shopify's
    real ISIN is the CA form; inside a US-listed ETF the US form is the far likelier answer,
    which is why the function names the assumption instead of hiding it.
    """
    assert derive_north_american_isin("82509L107", "US") == "US82509L1070"
    assert derive_north_american_isin("82509L107", "CA") == "CA82509L1076"


def test_a_non_north_american_prefix_is_a_programming_error_not_a_None():
    # Silently returning None here would let a caller think a German ISIN had simply failed to
    # derive, when in fact this module cannot derive one at all.
    with pytest.raises(ValueError):
        derive_north_american_isin("037833100", "DE")


@pytest.mark.parametrize("bad", ["", None, "12345", "67066G1040", "67066G10!", "  "])
def test_a_malformed_identifier_yields_none(bad):
    assert is_north_american_cusip(bad) is False
    if bad:
        assert derive_north_american_isin(bad) is None


def test_case_and_padding_are_tolerated():
    assert derive_north_american_isin(" 67066g104 ") == "US67066G1040"


@pytest.mark.parametrize("isin", [i for _, i in KNOWN] + ["NL0010273215", "IE00BMC38736"])
def test_looks_like_isin_accepts_real_ones(isin):
    assert looks_like_isin(isin) is True


@pytest.mark.parametrize("bad", [
    "US67066G1041",   # right shape, wrong check digit — the case a length test would pass
    "US67066G104",    # too short
    "US67066G10400",  # too long
    "US67066G104X",   # non-digit check position
    "1S67066G1040",   # digit in the country prefix
    None,
    "",
])
def test_looks_like_isin_rejects_what_only_resembles_one(bad):
    assert looks_like_isin(bad) is False


# Read out of QTUM's own basket on 2026-08-16, where 20 of 89 rows are SEDOLs sitting in a
# column headed "CUSIP".
SEDOLS = [
    ("B056381", "Global Unichip Corp"),
    ("6640400", "Nec Corp"),
    ("6051046", "Asustek Computer Inc"),
    ("4012250", "Airbus Group Se"),
    ("BZ1DZ96", "Reply Spa"),
]


@pytest.mark.parametrize("sedol,name", SEDOLS)
def test_a_sedol_is_recognised_by_its_own_check_digit(sedol, name):
    assert looks_like_sedol(sedol) is True, name
    assert identifier_kind(sedol) == "SEDOL"
    # And is never mistaken for something an ISIN can be derived from.
    assert derive_north_american_isin(sedol) is None


@pytest.mark.parametrize("bad", [
    "B056382",    # right shape, wrong check digit
    "CASHTWD",    # Invesco's cash placeholder: seven characters, but A is a vowel
    "670663A",    # vowel in the body again
    "B05638",     # too short
    "B0563810",   # too long
    None,
    "",
])
def test_looks_like_sedol_rejects_what_only_resembles_one(bad):
    assert looks_like_sedol(bad) is False


@pytest.mark.parametrize("value,kind", [
    ("US67066G1040", "ISIN"),
    ("67066G104", "CUSIP"),
    ("G29183103", "CINS"),
    ("B056381", "SEDOL"),
    # Real strings from the three files, none of which is an identifier at all. `BNYMLEND`
    # and `Cash&Other` would each be sent to OpenFIGI as a CUSIP by a length-only rule.
    ("BNYMLEND", None),
    ("Cash&Other", None),
    ("NVDA", None),
    ("3443 TT", None),
    (" -- ", None),
    ("", None),
    (None, None),
])
def test_identifier_kind_reads_the_shape_not_the_column_heading(value, kind):
    assert identifier_kind(value) == kind


def test_a_cash_pseudo_identifier_of_cusip_length_is_still_classified_as_cins():
    """
    Documents a limit rather than a capability: `CASHUSD00` is nine alphanumerics starting with
    a letter, so shape alone cannot tell it from a real CINS. It is harmless because its row is
    stated `Currency` by the issuer and never counts as invested — which is why the parsers
    attach an identifier only to rows that do.
    """
    assert identifier_kind("CASHUSD00") == "CINS"
