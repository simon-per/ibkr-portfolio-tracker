"""
`INGESTED_ATTRS` must list every Flex attribute the extractors actually read.

It decides whether a dropped attribute is loud or quiet, and the two failure directions
are not symmetric:

- **listing a field we do not read** only re-creates the noise this classification exists
  to remove — annoying, visible, self-correcting.
- **omitting a field we DO read** makes a real problem silent. That is the whole SBI
  class: a figure changes and nothing on the page says why.

So the map is not trusted. This derives the truth from the extractors' own source — an
AST walk for attribute access, intersected with ibflex's dataclass fields so ordinary
Python attributes (`.append`, `.isoformat`) and local names cannot masquerade as schema
— and fails if the map has drifted behind it.

Static, deliberately: it reads the source rather than running a sync, so it needs no
statement, no network and no fixture, and it still fails the day someone adds
`row.exDate` to an extractor without touching the map.
"""
import ast
import inspect

import pytest
from ibflex import Types

from app.services import ibkr_service
from app.services.ibkr_service import INGESTED_ATTRS, IBKRService, _is_ingested_attr

# Which ibflex element each extractor consumes. The one piece that cannot be derived:
# the extractors walk `statement.OpenPositions` etc. and the element type is implied by
# the container, not written down as a type.
EXTRACTOR_ELEMENT = {
    "extract_securities": "OpenPosition",
    "extract_taxlots": "OpenPosition",
    "extract_trades": "Trade",
    "extract_corporate_actions": "CorporateAction",
    "extract_cash_transactions": "CashTransaction",
    "extract_dividend_accruals": "OpenDividendAccrual",
    "extract_cash_flows": "CashTransaction",
    "extract_equity_summary": "EquitySummaryByReportDateInBase",
    "extract_cash_report": "CashReportCurrency",
    "extract_transfers": "Transfer",
    "parse_flex_xml": "FlexStatement",
}


def _attribute_names(func) -> set:
    """Every `x.name` and `getattr(x, "name")` appearing in a function's body."""
    src = inspect.getsource(func)
    tree = ast.parse(textwrap_dedent(src))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif (isinstance(node, ast.Call)
              and getattr(node.func, "id", "") == "getattr"
              and len(node.args) >= 2
              and isinstance(node.args[1], ast.Constant)
              and isinstance(node.args[1].value, str)):
            names.add(node.args[1].value)
    return names


def textwrap_dedent(src: str) -> str:
    import textwrap
    return textwrap.dedent(src)


def _model_fields(tag: str) -> set:
    """The attribute names ibflex actually models on that element."""
    cls = getattr(Types, tag)
    return set(getattr(cls, "__dataclass_fields__", {}))


@pytest.mark.parametrize("extractor,tag", sorted(EXTRACTOR_ELEMENT.items()))
def test_every_attribute_an_extractor_reads_is_declared_as_ingested(extractor, tag):
    read = _attribute_names(getattr(IBKRService, extractor)) & _model_fields(tag)
    missing = read - INGESTED_ATTRS.get(tag, frozenset())
    assert not missing, (
        f"{extractor}() reads {sorted(missing)} off <{tag}>, but INGESTED_ATTRS[{tag!r}] "
        f"does not list them. A drop of one of those would be classified cosmetic and "
        f"reported nowhere, while silently changing what is ingested."
    )


def test_the_map_lists_nothing_ibflex_does_not_model():
    """
    A name that is not a field on the element can never be dropped under that key, so it
    would sit in the map for ever doing nothing — and, worse, imply a coverage this file
    is not actually providing.
    """
    for tag, attrs in INGESTED_ATTRS.items():
        unknown = attrs - _model_fields(tag)
        assert not unknown, (
            f"INGESTED_ATTRS[{tag!r}] lists {sorted(unknown)}, which ibflex does not "
            f"model on <{tag}> — dead entries that cannot match a real drop."
        )


def test_every_element_the_extractors_consume_has_an_entry():
    """
    An element with no entry is cosmetic by definition, so a new extractor without a
    matching entry silences every drop on its element at once. This is the assertion
    that catches that, since the per-extractor test above would pass vacuously.
    """
    for extractor, tag in EXTRACTOR_ELEMENT.items():
        assert tag in INGESTED_ATTRS, (
            f"{extractor}() parses <{tag}> but INGESTED_ATTRS has no entry for it, so "
            f"every dropped attribute on that element is classified harmless."
        )


def test_every_extractor_is_covered_by_this_file():
    """
    `EXTRACTOR_ELEMENT` is the one thing here that cannot be derived — the element type
    is implied by the container an extractor walks, not written down. So it is
    hand-kept, and a hand-kept map is a map someone forgets: an extractor left out of it
    is not merely unchecked, it is *invisibly* unchecked, because the per-extractor test
    above is parametrized over this very map and simply never runs for it.

    That is the instance-versus-family distinction this codebase keeps relearning. Ask
    whether a copy exists at all, not whether the copies agree.
    """
    defined = {
        name for name in vars(IBKRService)
        if name.startswith("extract_") and callable(getattr(IBKRService, name))
    }
    missing = defined - set(EXTRACTOR_ELEMENT)
    assert not missing, (
        f"{sorted(missing)} parse a Flex element that this file never checks, so every "
        f"attribute they read could be dropped and classified cosmetic. Add each to "
        f"EXTRACTOR_ELEMENT with the ibflex element it consumes."
    )


def test_the_classifier_agrees_with_the_map():
    assert _is_ingested_attr("CashTransaction.type") is True
    assert _is_ingested_attr("Trade.tradePrice") is True
    # The 27 this account really drops on every sync — all cosmetic.
    for key in ("Trade.figi", "Trade.serialNumber", "Trade.subCategory", "Trade.weight",
                "Trade.notes", "Trade.commodityType", "Trade.initialInvestment",
                "CashTransaction.exDate", "CashTransaction.dividendType",
                "CashTransaction.actionID", "CashTransaction.issuerCountryCode"):
        assert _is_ingested_attr(key) is False, key
    # An element nothing parses is harmless by definition.
    assert _is_ingested_attr("SalesTax.taxableDescription") is False
    # Malformed keys must not raise; unknown is simply not ingested.
    assert _is_ingested_attr("nodot") is False


def test_ingested_attrs_is_not_quietly_empty():
    """The classification is only as good as the map; an empty one silences everything."""
    assert sum(len(v) for v in INGESTED_ATTRS.values()) > 50
    assert ibkr_service.INGESTED_ATTRS is INGESTED_ATTRS
