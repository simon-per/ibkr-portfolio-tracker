"""
Guards on `app/services/company_identity.py` — the grouping that makes one company one row.

Pure: no DB, no network, no clock. Every case below is a shape measured against this
account's real ISINs on 2026-08-14, so the fixtures are evidence rather than invention.

The two assertions that matter most are the ones a plausible implementation fails:

- **The unfold case.** A precedence chain (`lei or share_class_figi or isin`) splits a
  company whose members resolve to *different depths*, while reporting nothing wrong. Union
  over all identifiers is the only shape that does not.
- **Order independence.** The grouping and every display name must be byte-identical under a
  shuffled input, or a row's label and identity would depend on the order positions came
  back from the database.
"""
import random

import pytest

from app.etf_sources import ISSUER_OVERRIDES
from app.services.company_identity import (
    IdentityMember,
    IssuerOverride,
    company_groups,
    display_name,
)

# --- real identifiers, measured 2026-08-14 -----------------------------------------
ALPHABET_LEI = "5493006MHB84DD0ZWV18"
ALPHABET_A_ISIN = "US02079K3059"      # GOOGL @ NASDAQ, and ABEA @ IBIS
ALPHABET_C_ISIN = "US02079K1079"      # GOOG @ NASDAQ
ALPHABET_A_SCFIGI = "BBG009S39JY5"
ALPHABET_C_SCFIGI = "BBG009S3NB21"

ASML_LEI = "724500Y6DUVHQD6OXN27"
ASML_NY_ISIN = "USN070592100"         # NASDAQ line
ASML_AMS_ISIN = "NL0010273215"        # AEB line

TSMC_ORD_ISIN = "TW0002330008"        # GLEIF has NO record for this one
TSMC_ORD_SCFIGI = "BBG001S6Q004"
TSMC_ADR_ISIN = "US8740391003"
TSMC_ADR_SCFIGI = "BBG001S5WWW4"
TSMC_ADR_LEI = "549300KB6NK5SBD14S87"
TSMC_LEGAL_NAME_NON_LATIN = "台灣積體電路製造股份有限公司"


def _keys(groups):
    return {g.key for g in groups}


def _by_ref(groups):
    """ref -> group key, so membership can be compared without depending on ordering."""
    return {m.ref: g.key for g in groups for m in g.members}


def test_one_isin_on_two_exchanges_folds_with_no_identity_at_all():
    """
    GOOGL@NASDAQ and ABEA@IBIS are one ISIN, so the ISIN floor alone merges them.

    This is why the first slice of the feature ships value before any provider is called.
    """
    groups = company_groups([
        IdentityMember(ref="sec:1", isin=ALPHABET_A_ISIN, fallback_name="ALPHABET INC-CL A"),
        IdentityMember(ref="sec:2", isin=ALPHABET_A_ISIN, fallback_name="ALPHABET INC-CL A"),
    ])
    assert len(groups) == 1
    assert groups[0].key_type == "isin"
    assert groups[0].key == ALPHABET_A_ISIN
    assert len(groups[0].members) == 2


def test_two_share_classes_fold_on_the_lei_and_not_on_the_figi():
    """
    GOOG and GOOGL are different ISINs AND different shareClassFIGIs — as they must be, a
    share class being exactly what that identifier distinguishes. Only the LEI joins them,
    which is why GLEIF is not optional.
    """
    members = [
        IdentityMember(
            ref="sec:1", isin=ALPHABET_A_ISIN, share_class_figi=ALPHABET_A_SCFIGI,
            lei=ALPHABET_LEI, legal_name="ALPHABET INC.",
        ),
        IdentityMember(
            ref="sec:3", isin=ALPHABET_C_ISIN, share_class_figi=ALPHABET_C_SCFIGI,
            lei=ALPHABET_LEI, legal_name="ALPHABET INC.",
        ),
    ]
    groups = company_groups(members)
    assert len(groups) == 1
    assert groups[0].key == ALPHABET_LEI
    assert groups[0].key_type == "lei"
    assert groups[0].name == "ALPHABET INC."

    # Without the LEI they are genuinely two companies as far as any identifier knows.
    stripped = [
        IdentityMember(ref=m.ref, isin=m.isin, share_class_figi=m.share_class_figi)
        for m in members
    ]
    assert len(company_groups(stripped)) == 2


def test_one_company_with_two_isins_folds_on_the_lei():
    """ASML's NY registry share and its Amsterdam ordinary share no identifier but the LEI."""
    groups = company_groups([
        IdentityMember(ref="sec:4", isin=ASML_NY_ISIN, lei=ASML_LEI,
                       legal_name="ASML Holding N.V."),
        IdentityMember(ref="sec:5", isin=ASML_AMS_ISIN, lei=ASML_LEI,
                       legal_name="ASML Holding N.V."),
    ])
    assert len(groups) == 1
    assert groups[0].key == ASML_LEI
    assert groups[0].name == "ASML Holding N.V."


def test_a_precedence_chain_would_unfold_this_and_the_union_does_not():
    """
    THE case this module exists for.

    Two members share a shareClassFIGI, but only one of them also resolved an LEI. Under
    `lei or share_class_figi or isin` the first is keyed by its LEI and the second by the
    FIGI — two rows for one company, with the response reporting nothing amiss, because a
    *weaker* identifier had already joined them and the stronger one pulled them apart.
    """
    members = [
        IdentityMember(ref="fund:1#7", isin="XX0000000001",
                       share_class_figi=ALPHABET_A_SCFIGI),
        IdentityMember(ref="sec:1", isin=ALPHABET_A_ISIN,
                       share_class_figi=ALPHABET_A_SCFIGI, lei=ALPHABET_LEI,
                       legal_name="ALPHABET INC."),
    ]
    groups = company_groups(members)
    assert len(groups) == 1, (
        "a member sharing a shareClassFIGI must join the company even when it resolved no "
        "LEI of its own — this is the split a precedence chain produces"
    )
    # The stronger identifier still names the row.
    assert groups[0].key == ALPHABET_LEI
    assert groups[0].key_type == "lei"


def test_the_figi_alone_carries_the_isins_gleif_has_no_record_for():
    """
    GLEIF returns nothing for TSMC, Samsung, SK Hynix or Credo, so shareClassFIGI is the
    only tier that can fold their listings. Measured, not assumed.
    """
    groups = company_groups([
        IdentityMember(ref="sec:6", isin=TSMC_ORD_ISIN,
                       share_class_figi=TSMC_ORD_SCFIGI, figi_name="TAIWAN SEMICONDUCTOR MANUFAC"),
        IdentityMember(ref="fund:2#3", isin="XX0000000002",
                       share_class_figi=TSMC_ORD_SCFIGI),
    ])
    assert len(groups) == 1
    assert groups[0].key_type == "share_class_figi"
    assert groups[0].key == TSMC_ORD_SCFIGI


def test_an_adr_needs_the_declared_override_and_the_override_does_the_work():
    """
    No identifier joins TSMC's ADR to its ordinary, and both directions are checked here:
    without the override they are two rows, with it they are one. That is what proves the
    override is carrying the fold rather than masking a broken tier.
    """
    members = [
        IdentityMember(ref="sec:6", isin=TSMC_ORD_ISIN, share_class_figi=TSMC_ORD_SCFIGI),
        IdentityMember(ref="fund:3#1", isin=TSMC_ADR_ISIN,
                       share_class_figi=TSMC_ADR_SCFIGI, lei=TSMC_ADR_LEI),
    ]
    assert len(company_groups(members)) == 2, "no identifier joins an ADR to its ordinary"
    assert len(company_groups(members, ISSUER_OVERRIDES)) == 1, (
        "the declared override in app/etf_sources.py must fold TSMC's two lines"
    )


def test_an_override_is_symmetric_and_needs_no_holding_on_either_side():
    """
    Direction must not matter — the override is a statement about identity, not a pointer —
    and it has to be in force before the ADR ever appears in a basket.
    """
    overrides = {"AA0000000001": IssuerOverride(
        same_company_as_isin="BB0000000002", reason="test"
    )}
    forward = company_groups([
        IdentityMember(ref="a", isin="AA0000000001"),
        IdentityMember(ref="b", isin="BB0000000002"),
    ], overrides)
    backward = company_groups([
        IdentityMember(ref="b", isin="BB0000000002"),
        IdentityMember(ref="a", isin="AA0000000001"),
    ], overrides)
    assert len(forward) == len(backward) == 1
    assert forward[0].key == backward[0].key


def test_two_leis_in_one_group_are_reported_rather_than_silently_preferred():
    """
    Correct for a dual-listed company (Rio Tinto plc / Rio Tinto Ltd hold two legitimate
    LEIs) and identical in shape to a WRONG override. Reporting is the only honest
    response to a shape that is ambiguous by construction.
    """
    groups = company_groups([
        IdentityMember(ref="a", isin="AA0000000001", lei="LEI0000000000000001"),
        IdentityMember(ref="b", isin="BB0000000002", lei="LEI0000000000000002"),
    ], {"AA0000000001": IssuerOverride(
        same_company_as_isin="BB0000000002", reason="test"
    )})
    assert len(groups) == 1
    assert groups[0].override_conflicts, "two LEIs folded into one row must be reported"
    assert "LEI0000000000000001" in groups[0].override_conflicts[0]


def test_a_member_with_no_identifier_at_all_stands_alone_rather_than_vanishing():
    """
    Some issuers publish a name and a ticker but no ISIN. Such a row cannot be folded with
    anything, and guessing by name is exactly what this feature refuses to do — so it forms
    its own group, keyed on its ref so the row is stable between requests.
    """
    groups = company_groups([
        IdentityMember(ref="fund:4#2", fallback_name="SOME HOLDING PLC"),
        IdentityMember(ref="fund:4#3", fallback_name="ANOTHER HOLDING PLC"),
    ])
    assert len(groups) == 2
    assert {g.key_type for g in groups} == {"unidentified"}
    assert {g.name for g in groups} == {"SOME HOLDING PLC", "ANOTHER HOLDING PLC"}


def test_identifiers_are_case_and_whitespace_insensitive():
    """
    Issuer files disagree about ISIN casing. A lower-cased ISIN failing to match its
    upper-cased sibling would silently unfold a company — the quiet half of this feature's
    failure mode.
    """
    groups = company_groups([
        IdentityMember(ref="a", isin=ALPHABET_A_ISIN),
        IdentityMember(ref="b", isin=f"  {ALPHABET_A_ISIN.lower()} "),
    ])
    assert len(groups) == 1


def test_partially_resolved_marks_the_rows_that_may_still_be_short():
    """A member folded on its ISIN alone means an unseen sibling ISIN would have missed."""
    resolved = company_groups([
        IdentityMember(ref="a", isin=ASML_NY_ISIN, lei=ASML_LEI),
        IdentityMember(ref="b", isin=ASML_AMS_ISIN, lei=ASML_LEI),
    ])
    assert resolved[0].partially_resolved is False

    partial = company_groups([
        IdentityMember(ref="a", isin=ASML_NY_ISIN, lei=ASML_LEI),
        IdentityMember(ref="b", isin=ASML_NY_ISIN),
    ])
    assert partial[0].partially_resolved is True


# --------------------------------------------------------------------- display names

def test_a_non_latin_legal_name_loses_to_the_figi_name():
    """
    GLEIF's legal name for TSMC is 台灣積體電路製造股份有限公司 — correct, and unreadable on a
    dashboard whose every other row is Latin. OpenFIGI's truncated ASCII label wins here.
    """
    group = company_groups([
        IdentityMember(
            ref="sec:6", isin=TSMC_ORD_ISIN, share_class_figi=TSMC_ORD_SCFIGI,
            legal_name=TSMC_LEGAL_NAME_NON_LATIN,
            figi_name="TAIWAN SEMICONDUCTOR MANUFAC",
            fallback_name="TAIWAN SEMICONDUCTOR MANUFACTURING",
        )
    ])[0]
    assert group.name == "TAIWAN SEMICONDUCTOR MANUFAC"


def test_a_latin_legal_name_beats_the_truncated_figi_name():
    group = company_groups([
        IdentityMember(
            ref="sec:4", isin=ASML_NY_ISIN, lei=ASML_LEI,
            legal_name="ASML Holding N.V.", figi_name="ASML HOLDING NV",
        )
    ])[0]
    assert group.name == "ASML Holding N.V."


def test_accented_latin_names_are_not_mistaken_for_another_script():
    """`Société Générale` must keep its own name, so the check cannot simply be `isascii()`."""
    group = company_groups([
        IdentityMember(ref="a", isin="FR0000130809", lei="O2RNE8IBXP4R0TD8PU41",
                       legal_name="SOCIETE GENERALE Société anonyme",
                       figi_name="SOCIETE GENERALE")
    ])[0]
    assert group.name.startswith("SOCIETE GENERALE Soci")


def test_a_non_latin_legal_name_still_beats_nothing():
    """The truth, hard to read, is better than a ref."""
    group = company_groups([
        IdentityMember(ref="x", isin="XX0000000009",
                       legal_name=TSMC_LEGAL_NAME_NON_LATIN)
    ])[0]
    assert group.name == TSMC_LEGAL_NAME_NON_LATIN


def test_the_name_comes_from_the_largest_contributor_not_the_first():
    """
    An issuer's `issueName` differs per file ("ALPHABET INC CLASS A" vs "Alphabet Inc."), so
    the label has to be chosen deterministically or it would change with sort order.
    """
    small = IdentityMember(ref="fund:1#9", isin=ALPHABET_A_ISIN,
                           fallback_name="ALPHABET INC CLASS A", weight=1)
    big = IdentityMember(ref="sec:1", isin=ALPHABET_A_ISIN,
                         fallback_name="Alphabet Inc.", weight=1000)
    assert company_groups([small, big])[0].name == "Alphabet Inc."
    assert company_groups([big, small])[0].name == "Alphabet Inc."


# ------------------------------------------------------------------ order independence

def test_grouping_and_naming_are_byte_identical_under_a_shuffled_input():
    """
    The whole output must be a function of the members, not of their order. Positions arrive
    from one query and constituents from another, and neither ordering is guaranteed stable.
    """
    members = [
        IdentityMember(ref="sec:1", isin=ALPHABET_A_ISIN, share_class_figi=ALPHABET_A_SCFIGI,
                       lei=ALPHABET_LEI, legal_name="ALPHABET INC.", weight=3378),
        IdentityMember(ref="sec:2", isin=ALPHABET_A_ISIN, weight=281),
        IdentityMember(ref="sec:3", isin=ALPHABET_C_ISIN, share_class_figi=ALPHABET_C_SCFIGI,
                       lei=ALPHABET_LEI, legal_name="ALPHABET INC.", weight=1398),
        IdentityMember(ref="sec:4", isin=ASML_NY_ISIN, lei=ASML_LEI,
                       legal_name="ASML Holding N.V.", weight=4505),
        IdentityMember(ref="sec:5", isin=ASML_AMS_ISIN, lei=ASML_LEI,
                       legal_name="ASML Holding N.V.", weight=1499),
        IdentityMember(ref="sec:6", isin=TSMC_ORD_ISIN, share_class_figi=TSMC_ORD_SCFIGI,
                       legal_name=TSMC_LEGAL_NAME_NON_LATIN,
                       figi_name="TAIWAN SEMICONDUCTOR MANUFAC", weight=1454),
        IdentityMember(ref="fund:x#1", fallback_name="NO IDENTIFIER PLC", weight=12),
    ]
    baseline = company_groups(members, ISSUER_OVERRIDES)
    baseline_shape = (
        [(g.key, g.key_type, g.name) for g in baseline],
        _by_ref(baseline),
    )

    rng = random.Random(20260814)
    for _ in range(25):
        shuffled = members[:]
        rng.shuffle(shuffled)
        groups = company_groups(shuffled, ISSUER_OVERRIDES)
        assert (
            [(g.key, g.key_type, g.name) for g in groups],
            _by_ref(groups),
        ) == baseline_shape


def test_the_expected_folds_hold_on_this_accounts_real_shape():
    """A end-to-end sanity check on the six holdings the feature was motivated by."""
    groups = company_groups([
        IdentityMember(ref="sec:1", isin=ALPHABET_A_ISIN, lei=ALPHABET_LEI,
                       legal_name="ALPHABET INC.", weight=3378),
        IdentityMember(ref="sec:2", isin=ALPHABET_A_ISIN, lei=ALPHABET_LEI,
                       legal_name="ALPHABET INC.", weight=281),
        IdentityMember(ref="sec:3", isin=ALPHABET_C_ISIN, lei=ALPHABET_LEI,
                       legal_name="ALPHABET INC.", weight=1398),
        IdentityMember(ref="sec:4", isin=ASML_NY_ISIN, lei=ASML_LEI,
                       legal_name="ASML Holding N.V.", weight=4505),
        IdentityMember(ref="sec:5", isin=ASML_AMS_ISIN, lei=ASML_LEI,
                       legal_name="ASML Holding N.V.", weight=1499),
    ])
    assert _keys(groups) == {ALPHABET_LEI, ASML_LEI}
    totals = {g.key: sum(m.weight for m in g.members) for g in groups}
    assert totals[ALPHABET_LEI] == 5057   # 3378 + 281 + 1398
    assert totals[ASML_LEI] == 6004       # 4505 + 1499
    # Sorted by value descending, so ASML leads.
    assert [g.key for g in groups] == [ASML_LEI, ALPHABET_LEI]


def test_display_name_is_callable_on_a_group_directly():
    """It is part of the module's surface, and `company_groups` must not be the only caller."""
    group = company_groups([IdentityMember(ref="z", isin="XX0000000003")])[0]
    assert display_name(group) == "z"
