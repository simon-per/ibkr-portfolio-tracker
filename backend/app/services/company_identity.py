"""
Group listings and share classes of one company into a single row.

Three of the four largest true positions in this account are invisible in the Positions
table because one company is spread across several rows, and the three shapes are all
different — which is why this cannot be a string match on names or symbols:

    GOOGL@NASDAQ + ABEA@IBIS      one ISIN (US02079K3059), two exchanges
    GOOG@NASDAQ                   a DIFFERENT ISIN (US02079K1079), share class C
    ASML@NASDAQ + ASML@AEB        two different ISINs (USN070592100 NY registry,
                                  NL0010273215 Amsterdam ordinary), one company

**Grouping is a union, never a precedence chain, and that distinction is the whole
point of this module.** The obvious design is `key = lei or share_class_figi or isin`,
and it re-creates the bug the feature exists to fix: if one of a company's ISINs
resolves an LEI and its sibling does not, the first is keyed by its LEI and the second
by its ISIN, so they land in *two rows* — while the response cheerfully reports "1
unresolved". A weaker identifier had already folded them and the stronger one pulled
them apart. So two members belong to the same company when they share **any**
identifier, and the group's key is chosen afterwards purely so the API has a stable id.

Which identifier does which job was measured against this account's own ISINs
(2026-08-14), and neither provider is sufficient alone:

- **LEI (GLEIF)** folds share classes and multi-ISIN companies. Both Alphabet ISINs
  return 5493006MHB84DD0ZWV18, and both ASML ISINs return 724500Y6DUVHQD6OXN27.
- **shareClassFIGI (OpenFIGI)** folds one share class across venues, and is the *only*
  tier that resolves the Taiwanese and Korean ordinaries held here: GLEIF has no ISIN
  record at all for TSMC (TW0002330008), Samsung (KR7005930003), SK Hynix
  (KR7000660001) or Credo (KYG254571055), and OpenFIGI resolves all four.
- **ISIN** is the floor, so grouping degrades gracefully and a company is never lost.
  It alone already folds GOOGL+ABEA and AMZN+AMZ, which is why the first slice of this
  feature needs no network call.

`compositeFIGI` is deliberately **not** an input. It is per-exchange-composite, so it
folds nothing that the ISIN does not already fold, and including it would silently
merge unrelated lines. It is stored on `isin_identity` for diagnosis only.

**What no identifier can fold** — and hence `ISSUER_OVERRIDES` in `app/etf_sources.py`:
an ADR against its ordinary (TSMC's ordinary is shareClassFIGI BBG001S6Q004 while the
TSM ADR is BBG001S5WWW4, and GLEIF gives the ADR a LEI while giving the ordinary none),
and dual-listed companies such as Rio Tinto plc / Rio Tinto Ltd, which carry two
legitimate LEIs by design.

Pure: no DB, no network, no clock. Everything here is a function of its arguments, so
`tests/test_company_identity.py` can shuffle the input and assert the output is
byte-identical.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

# Anything at or above this codepoint is outside the Latin blocks (Basic Latin, Latin-1
# Supplement, Latin Extended-A/B, IPA, spacing modifiers, combining diacriticals) and
# so starts Greek/Cyrillic/CJK/Hangul/Hebrew/Arabic. Used only to pick a *display*
# name: GLEIF returns the legal name in its own script, so TSMC's is
# 台灣積體電路製造股份有限公司 — correct, and unreadable on a dashboard whose every other
# row is Latin. Deliberately permissive about accents, so "Société Générale" and
# "Nestlé" keep their own names rather than falling back to a truncated FIGI label.
_NON_LATIN_START = 0x0370


def _is_latin(text: str) -> bool:
    """True when every letter in `text` sits in a Latin block."""
    return all(ord(ch) < _NON_LATIN_START for ch in text if ch.isalpha())


@dataclass(frozen=True)
class IssuerOverride:
    """
    A hand-declared statement that two ISINs are the same company.

    `reason` is mandatory and non-empty by test, because this is the one tier with no
    external evidence behind it — the same discipline `MINOR_UNIT_CURRENCIES` and
    `TICKER_CURRENCY_OVERRIDES` follow. Growth here is the signal that a tier below is
    being misused rather than that the world got more complicated.
    """
    same_company_as_isin: str
    reason: str


@dataclass(frozen=True)
class IdentityMember:
    """
    One thing that might be part of a company: a directly-held security, or one
    constituent row of one fund.

    `ref` is an opaque caller-side handle ("sec:12", "IE00BMFKG444#41") echoed back on
    the group so the caller can attribute value without this module knowing what a
    position is. `weight` orders members when a display name has to be chosen, and is
    the only reason it is here — a label that changes with sort order is precisely this
    codebase's signature class of bug.
    """
    ref: str
    isin: Optional[str] = None
    share_class_figi: Optional[str] = None
    lei: Optional[str] = None
    legal_name: Optional[str] = None
    figi_name: Optional[str] = None
    fallback_name: Optional[str] = None
    weight: Decimal = Decimal("0")


@dataclass
class CompanyGroup:
    """One company, and the members found to be part of it."""
    key: str
    key_type: str  # 'lei' | 'share_class_figi' | 'isin' | 'unidentified'
    name: str
    members: List[IdentityMember] = field(default_factory=list)
    isins: List[str] = field(default_factory=list)
    leis: List[str] = field(default_factory=list)
    share_class_figis: List[str] = field(default_factory=list)
    override_conflicts: List[str] = field(default_factory=list)

    @property
    def partially_resolved(self) -> bool:
        """
        True when at least one member carries neither an LEI nor a shareClassFIGI, so
        it could only have been folded on its own ISIN.

        That is the honest caveat on a row: an unseen sibling holding a *different*
        ISIN would not have joined this group, so the row may still be understated.
        Reported per row rather than as a global count, because "3 unresolved" says
        nothing about which company is short.
        """
        return any(not m.lei and not m.share_class_figi for m in self.members)


class _DisjointSet:
    """
    Union-find over opaque string labels.

    Unions toward the lexicographically smaller label so the forest itself is
    independent of the order unions arrive in. The group key is derived from a
    component's identifiers rather than from its root anyway, so this is belt and
    braces — but it makes the intermediate state reproducible, which is what makes a
    failure in `test_company_identity.py` readable.
    """

    def __init__(self) -> None:
        self._parent: Dict[str, str] = {}

    def add(self, label: str) -> None:
        self._parent.setdefault(label, label)

    def find(self, label: str) -> str:
        self.add(label)
        root = label
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression, iterative: these components are shallow but a feeder fund
        # holding its own share class can chain, and recursion here would be a
        # stack-depth surprise on a 10,000-row basket.
        while self._parent[label] != root:
            self._parent[label], label = root, self._parent[label]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        self._parent[rb] = ra


def _clean(value: Optional[str]) -> Optional[str]:
    """Normalise an identifier: identifiers are case-insensitive and whitespace-free.

    Upper-casing matters — an ISIN arriving lower-cased from one issuer file and
    upper-cased from another would fail to fold, which is the silent half of this
    feature's failure mode."""
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def company_groups(
    members: Iterable[IdentityMember],
    overrides: Optional[Mapping[str, IssuerOverride]] = None,
) -> List[CompanyGroup]:
    """
    Fold `members` into companies, unioning any two that share an identifier.

    Returns groups sorted by descending total weight then ascending key, so the
    ordering is total and cannot shuffle between requests when thousands of
    constituents tie near zero.
    """
    members = list(members)
    overrides = overrides or {}
    dsu = _DisjointSet()

    # A member is a node in its own right, so a member with no identifiers at all still
    # forms a group of one rather than vanishing.
    def member_node(index: int) -> str:
        return f"member:{index}"

    normalised: List[Tuple[Optional[str], Optional[str], Optional[str]]] = []
    for index, member in enumerate(members):
        isin = _clean(member.isin)
        scfigi = _clean(member.share_class_figi)
        lei = _clean(member.lei)
        normalised.append((isin, scfigi, lei))

        node = member_node(index)
        dsu.add(node)
        for label in (
            f"isin:{isin}" if isin else None,
            f"scfigi:{scfigi}" if scfigi else None,
            f"lei:{lei}" if lei else None,
        ):
            if label:
                dsu.union(node, label)

    # Overrides join two ISIN nodes directly, so they apply whether or not either side
    # is actually held right now — the TSM ADR only appears once a basket is imported,
    # and the override must already be in force when it does.
    for raw_isin, override in overrides.items():
        isin = _clean(raw_isin)
        target = _clean(override.same_company_as_isin)
        if isin and target and isin != target:
            dsu.union(f"isin:{isin}", f"isin:{target}")

    by_root: Dict[str, List[int]] = {}
    for index in range(len(members)):
        by_root.setdefault(dsu.find(member_node(index)), []).append(index)

    groups: List[CompanyGroup] = []
    for indices in by_root.values():
        group_members = [members[i] for i in indices]
        isins = sorted({normalised[i][0] for i in indices if normalised[i][0]})
        scfigis = sorted({normalised[i][1] for i in indices if normalised[i][1]})
        leis = sorted({normalised[i][2] for i in indices if normalised[i][2]})

        if leis:
            key, key_type = leis[0], "lei"
        elif scfigis:
            key, key_type = scfigis[0], "share_class_figi"
        elif isins:
            key, key_type = isins[0], "isin"
        else:
            # No identifier at all. Keyed on the member's own ref so the row is stable
            # across requests; it can never merge with anything, which is correct —
            # guessing would be worse than showing it alone.
            key, key_type = min(m.ref for m in group_members), "unidentified"

        group = CompanyGroup(
            key=key,
            key_type=key_type,
            name="",
            members=group_members,
            isins=isins,
            leis=leis,
            share_class_figis=scfigis,
        )
        # Two distinct LEIs inside one group means an override (or a shared
        # shareClassFIGI) has merged what GLEIF considers two legal entities. That can
        # be entirely correct — Rio Tinto is exactly this — but it must be *reported*
        # rather than silently preferred, because the same shape is what a wrong
        # override looks like.
        if len(leis) > 1:
            group.override_conflicts.append(
                f"{len(leis)} distinct LEIs folded into one company: {', '.join(leis)}"
            )
        group.name = display_name(group)
        groups.append(group)

    groups.sort(key=lambda g: (-sum(m.weight for m in g.members), g.key))
    return groups


def display_name(group: CompanyGroup) -> str:
    """
    Pick the label for a company, deterministically.

    GLEIF's legal name is the best answer for Western companies ("ASML Holding N.V.",
    "AMAZON.COM, INC.") and unusable for Asian ones, where it arrives in its own script.
    OpenFIGI's is clean ASCII but truncated to ~30 characters
    ("TAIWAN SEMICONDUCTOR MANUFAC"), and an issuer file's `issueName` differs per
    issuer ("ALPHABET INC CLASS A" vs "Alphabet Inc."). So the order is: a Latin-script
    legal name, else the FIGI name, else a non-Latin legal name (still the truth, just
    hard to read), else the highest-weight contributing row's own name, else the ref.

    Members are walked in one canonical order — weight descending, ref ascending — for
    every candidate, so the label cannot change with the caller's iteration order.
    """
    ordered = sorted(group.members, key=lambda m: (-m.weight, m.ref))

    def first(pick) -> Optional[str]:
        for member in ordered:
            value = (pick(member) or "").strip()
            if value:
                return value
        return None

    latin_legal = next(
        (
            (m.legal_name or "").strip()
            for m in ordered
            if (m.legal_name or "").strip() and _is_latin(m.legal_name)
        ),
        None,
    )
    return (
        latin_legal
        or first(lambda m: m.figi_name)
        or first(lambda m: m.legal_name)
        or first(lambda m: m.fallback_name)
        or ordered[0].ref
    )
