"""
The nine- and seven-character identifiers US ETF issuers publish, and which of them an ISIN
can be derived from locally — the cases where that must *not* be attempted being the point.

US ETF issuers publish their daily baskets keyed by CUSIP, never by ISIN, while everything in
this app's identity pipeline is keyed by ISIN. A North American ISIN is just its CUSIP with a
country prefix and a check digit, so the conversion is arithmetic rather than a lookup: no API
call, no key, no rate limit, and no provider to be wrong about it.

**The trap is that not every nine-character identifier in those files is a CUSIP.** They are
CINS — the same numbering space extended to foreign issuers, where a *leading letter* encodes the
country. Measured in QTUM's own basket on 2026-08-16: 61 of 68 rows are digit-leading, and 7 are
not (`N` Netherlands, `G` Cayman/Ireland, `Y` Asia, `M`). GRID's largest holdings are
`G29183103` (Eaton) and `F86921107` (Schneider). Prefixing either with `US` yields a
*syntactically valid* ISIN — correct check digit, right shape — that belongs to nothing. So the
letter-leading ones are refused here and resolved through OpenFIGI's `ID_CUSIP` mapping instead.

**And digit-leading is not unambiguously US.** Canadian issuers are assigned CUSIPs from the same
numeric space, so `82509L107` yields both `US82509L1070` and `CA82509L1076` and only one exists.
The `US` form is the overwhelmingly likelier answer inside a US-listed ETF, so that is what
`derive_north_american_isin` returns — but it says so in its name, the caller records the
provenance, and a derived ISIN must never outrank a published one.

**A third shape shares the column.** Defiance publishes 20 **SEDOLs** among QTUM's 89 rows
(`B056381` Global Unichip, `6640400` NEC, `4012250` Airbus) — seven characters, no vowels, its
own check digit. Nothing about the column heading says so; it is called "CUSIP". So
`identifier_kind` classifies by shape rather than by what the file claims, and the callers use
it to pick OpenFIGI's `idType` — sending a SEDOL as `ID_CUSIP` resolves nothing.

The failure mode is deliberately the safe direction: a wrong derivation produces an ISIN that
matches nothing, so the company stands alone as its own row (an understatement) rather than
merging into some other company's (a fabrication).
"""
from typing import Optional

CUSIP_LENGTH = 9
SEDOL_LENGTH = 7
ISIN_LENGTH = 12

# SEDOL excludes vowels from its first six characters, which is what tells one from a
# seven-character cash pseudo-identifier: Invesco's `CASHTWD` has an A in it.
_SEDOL_VOWELS = frozenset("AEIOU")
# The published SEDOL weights, applied left to right over the six body characters.
_SEDOL_WEIGHTS = (1, 3, 1, 7, 3, 9)

# The only prefixes this module will produce. Both are the North American CUSIP space; anything
# else needs a real lookup.
NORTH_AMERICAN_PREFIXES = ("US", "CA")


def _luhn_check_digit(body: str) -> str:
    """
    The ISIN check digit: expand letters to their base-36 value, then mod-10 double-add-double
    from the right. Shared by the ISIN and CUSIP standards, which is why it is written once.
    """
    digits = ""
    for char in body:
        digits += str(int(char, 36)) if char.isalpha() else char

    total = 0
    double = True
    for char in reversed(digits):
        value = int(char) * (2 if double else 1)
        double = not double
        total += value // 10 + value % 10
    return str((10 - total % 10) % 10)


def is_north_american_cusip(cusip: Optional[str]) -> bool:
    """
    True only for a nine-character identifier in the North American (digit-leading) space.

    A leading letter means CINS — a foreign issuer inside the same numbering scheme — and those
    are exactly the ones a `US` prefix would silently mis-derive.
    """
    if not cusip:
        return False
    value = cusip.strip().upper()
    if len(value) != CUSIP_LENGTH or not value.isalnum():
        return False
    return value[0].isdigit()


def derive_north_american_isin(cusip: str, country: str = "US") -> Optional[str]:
    """
    The ISIN a North American CUSIP implies, or None when the input is not one.

    Returns None rather than raising because it sits inside a per-row parse loop where one
    unusable identifier must cost that row its fold, not the whole file — the opposite of the
    refuse-the-file-whole rule, which applies to *malformed* data rather than to data that is
    simply not derivable.
    """
    if country not in NORTH_AMERICAN_PREFIXES:
        raise ValueError(f"{country!r} is not a North American prefix; use OpenFIGI instead")
    if not is_north_american_cusip(cusip):
        return None
    body = country + cusip.strip().upper()
    return body + _luhn_check_digit(body)


def cusip_from_isin(isin: Optional[str]) -> Optional[str]:
    """
    The CUSIP inside a North American ISIN, or None if there is not one.

    The exact inverse of `derive_north_american_isin`, and it exists for one caller: Invesco's
    holdings endpoint is keyed by the *fund's* CUSIP, so this turns the registry's fund ISIN
    into the URL without anyone having to record a second identifier per fund — the kind of
    hand-discovered id that BlackRock's `portfolio_id` shows the cost of.

    Round-tripped rather than sliced: a string can carry a `US` prefix and still not be an
    ISIN, and quietly slicing one would build a URL for a fund that does not exist.
    """
    if not looks_like_isin(isin):
        return None
    candidate = isin.strip().upper()
    if candidate[:2] not in NORTH_AMERICAN_PREFIXES:
        return None
    body = candidate[2:-1]
    return body if derive_north_american_isin(body, candidate[:2]) == candidate else None


def looks_like_isin(value: Optional[str]) -> bool:
    """
    Whether a string is a well-formed ISIN, check digit included.

    Used to tell an issuer file that publishes real ISINs (VanEck) from one that publishes
    CUSIPs (Invesco, First Trust, Defiance) without trusting the column heading — and to refuse
    a derived value that came out malformed.
    """
    if not value:
        return False
    candidate = value.strip().upper()
    if len(candidate) != ISIN_LENGTH or not candidate.isalnum():
        return False
    if not candidate[:2].isalpha() or not candidate[-1].isdigit():
        return False
    return _luhn_check_digit(candidate[:-1]) == candidate[-1]


def looks_like_sedol(value: Optional[str]) -> bool:
    """
    Whether a string is a well-formed SEDOL, check digit included.

    The check digit is what makes this worth doing: without it, "seven alphanumerics with no
    vowels" would also accept a truncated CUSIP and hand OpenFIGI the wrong `idType`.
    """
    if not value:
        return False
    candidate = value.strip().upper()
    if len(candidate) != SEDOL_LENGTH or not candidate.isalnum():
        return False
    body, check = candidate[:-1], candidate[-1]
    if not check.isdigit() or any(char in _SEDOL_VOWELS for char in body):
        return False
    total = sum(int(char, 36) * weight for char, weight in zip(body, _SEDOL_WEIGHTS))
    return str((10 - total % 10) % 10) == check


def identifier_kind(value: Optional[str]) -> Optional[str]:
    """
    What kind of identifier this is, read off its shape rather than off a column heading.

    Returns `ISIN` | `CUSIP` | `CINS` | `SEDOL`, or None for anything else — a ticker, a cash
    placeholder like `Cash&Other`, an issuer's internal code like `BNYMLEND`. The callers use
    it twice: the parsers decide whether an ISIN can be derived, and identity resolution picks
    OpenFIGI's `idType` from it.

    ISIN is tested first because a valid ISIN is never any of the others, and CUSIP before CINS
    because the two differ only in whether the first character is a digit.
    """
    if not value:
        return None
    candidate = value.strip().upper()
    if looks_like_isin(candidate):
        return "ISIN"
    if len(candidate) == CUSIP_LENGTH and candidate.isalnum():
        return "CUSIP" if candidate[0].isdigit() else "CINS"
    if looks_like_sedol(candidate):
        return "SEDOL"
    return None
