"""
The CoinStats parse helpers have one home, `app/services/coinstats_parse.py` (issue #34).

They were written three times — `crypto_service`, the rebuild CLI and the probe — and the
copies already disagreed: the probe read `{"USD": True}` as 1.0 and `True` as 1970-01-01,
refused `{"USD": "12.5"}`, raised on an out-of-range epoch and rendered lists without their
item shape. The probe is the tool that decides how the sync should read a payload, so a
probe that reads differently from the sync measures the wrong thing.

Two halves:

1. **Is there a copy at all** — walk every crypto module (the list
   `test_crypto_isolation.py` owns) and fail on a function named like one of these
   helpers defined anywhere but `coinstats_parse`; and at most one `sum_known`.
2. **The single implementation** — the cases that exposed the drift, now as unit tests,
   with the sync's former parsing as the expected answer.
"""
import ast
import math
import re
from datetime import datetime

import pytest

from app.cli import coinstats_probe, crypto_rebuild_holdings
from app.services import coinstats_parse, crypto_service
from app.services.coinstats_parse import num, shape, to_utc_datetime, usd, usd_period
from tests.test_crypto_isolation import APP_DIR, CRYPTO_MODULES

PARSE_MODULE = "app.services.coinstats_parse"

# Function names that mean "a CoinStats parse helper", with or without a leading `_`.
PARSE_HELPER_NAME = re.compile(
    r"^_?(num|usd|usd_period|shape)$|to_\w*datetime|to_utc"
)
SUM_KNOWN_NAME = re.compile(r"sum_known")


def _path(module: str):
    return APP_DIR.parent.joinpath(*module.split(".")).with_suffix(".py")


def _function_names(source: str):
    return [
        node.name for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _parse_helper_copies(modules):
    """{module: [names]} of parse helpers defined outside the parse module."""
    found = {}
    for module, source in modules.items():
        if module == PARSE_MODULE:
            continue
        names = [n for n in _function_names(source) if PARSE_HELPER_NAME.search(n)]
        if names:
            found[module] = names
    return found


def _crypto_sources():
    return {m: _path(m).read_text(encoding="utf-8") for m in sorted(CRYPTO_MODULES)}


# ───────────────────────────────────────────────────────────── is there a copy at all


def test_the_parse_module_is_a_crypto_module():
    assert PARSE_MODULE in CRYPTO_MODULES


def test_no_crypto_module_defines_its_own_parse_helper():
    copies = _parse_helper_copies(_crypto_sources())
    assert not copies, (
        f"CoinStats parse helpers defined outside {PARSE_MODULE}: {copies}. Import them "
        f"from there — a second copy is how the probe came to read payloads differently "
        f"from the sync (issue #34)."
    )


def test_one_sum_where_an_unknown_makes_the_sum_unknown():
    defs = [
        (m, n) for m, source in _crypto_sources().items()
        for n in _function_names(source) if SUM_KNOWN_NAME.search(n)
    ]
    assert len(defs) <= 1, defs


def test_the_walk_finds_the_issue_34_shapes():
    old = {
        "app.cli.coinstats_probe": (
            "def _shape(value, depth=0): ...\n"
            "def _to_datetime(ts): ...\n"
            "def _usd(block): ...\n"
        ),
        "app.services.crypto_service": (
            "def _num(value): ...\n"
            "def _usd_period(block, period): ...\n"
            "class X:\n    def _to_utc_date(self, s): ...\n"
        ),
        PARSE_MODULE: "def num(value): ...\n",
    }
    assert _parse_helper_copies(old) == {
        "app.cli.coinstats_probe": ["_shape", "_to_datetime", "_usd"],
        "app.services.crypto_service": ["_num", "_usd_period", "_to_utc_date"],
    }


def test_the_sync_the_rebuild_and_the_probe_use_the_same_objects():
    assert crypto_service.num is crypto_rebuild_holdings.num is coinstats_probe.num is num
    assert crypto_service.usd is coinstats_probe.usd is usd
    assert crypto_rebuild_holdings.to_utc_datetime is coinstats_probe.to_utc_datetime
    assert crypto_rebuild_holdings.shape is coinstats_probe.shape is shape
    assert crypto_service.usd_period is usd_period
    assert coinstats_parse.to_utc_datetime is to_utc_datetime


# ───────────────────────────────────────────────────────────── the one implementation


@pytest.mark.parametrize("value, expected", [
    (True, None),              # a bool is not a number
    (False, None),
    ("12.5", 12.5),            # numeric strings are
    ("abc", None),
    (float("nan"), None),      # not finite
    (float("inf"), None),
    ("inf", None),
    (None, None),
    (3, 3.0),
    ([1], None),
])
def test_num(value, expected):
    assert num(value) == expected


@pytest.mark.parametrize("block, expected", [
    ({"USD": "12.5"}, 12.5),          # numeric string
    ({"USD": True}, None),            # a bool is not a number
    ({"USD": float("nan")}, None),    # not finite
    (5, 5.0),                         # bare number
    ({"EUR": 4.0}, None),             # no USD figure
    (None, None),
])
def test_usd(block, expected):
    result = usd(block)
    if expected is None:
        assert result is None
    else:
        assert math.isclose(result, expected)


def test_usd_period():
    profit = {"allTime": {"USD": 7.0}, "hour24": {"USD": "x"}}
    assert usd_period(profit, "allTime") == 7.0
    assert usd_period(profit, "hour24") is None
    assert usd_period(profit, "unrealized") is None
    assert usd_period(7.0, "allTime") is None


@pytest.mark.parametrize("stamp, expected", [
    (True, None),                                          # a bool is not a timestamp
    (10 ** 20, None),                                      # out of range: None, no raise
    ("2026-01-02T03:04:05+02:00", datetime(2026, 1, 2, 1, 4, 5)),
    ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5)),
    ("2026-01-02T03:04:05", datetime(2026, 1, 2, 3, 4, 5)),
    (1_767_323_045, datetime(2026, 1, 2, 3, 4, 5)),       # epoch seconds
    (1_767_323_045_000, datetime(2026, 1, 2, 3, 4, 5)),   # epoch milliseconds
    ("not a date", None),
    (None, None),
])
def test_to_utc_datetime_is_naive_utc(stamp, expected):
    result = to_utc_datetime(stamp)
    assert result == expected
    assert result is None or result.tzinfo is None


def test_shape_names_keys_and_types_never_values():
    assert shape({"items": [{"a": 1}]}) == "{items: list[1] of {...}}"
    assert shape([{"a": 1, "b": "secret"}]) == "list[1] of {a: int, b: str}"
    assert shape([]) == "list[0]"
    assert "secret" not in shape({"note": "secret"})
