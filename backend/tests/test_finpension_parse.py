"""
The finpension parser, and the Balance oracle in particular.

Everything here is synthetic. The real export is account data and gitignored, and a
fixture built by hand is what lets the failure cases be exercised at all — the
interesting ones are files finpension would never produce.

Offline: no network, no DB.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services.finpension_report import (
    CATEGORY_KINDS,
    DEPOSIT,
    EXPECTED_HEADER,
    FEE,
    INCOME,
    LIQUIDATION,
    TRADE,
    TRANSFER_IN,
    FinpensionParseError,
    parse_transaction_report,
)


HEADER = ";".join(EXPECTED_HEADER)

# The two funds this account actually holds, with their real names — which is the
# point of the quoting test: both contain parentheses, and one contains a comma-free
# but space-heavy name that a naive split would still mangle.
EM = "CH1529078078"
WORLD = "CH0117044948"
EM_NAME = "Swisscanto (CH) Index Equity Fund Emerging Markets NMT CHF"
WORLD_NAME = "Swisscanto (CH) IPF I Index Equity Fund World ex CH NT CHF"


def _row(d, category, cash, balance, *, name="", isin="", shares="",
         ccy="CHF", rate="1.0000000000", price=""):
    return (f'{d};{category};"{name}";{isin};{shares};{ccy};{rate};'
            f'{price};{cash};{balance}')


def _file(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


def _realistic():
    """The real file's shape, with its real numbers — three deposits then two buys."""
    return _file(
        _row("2026-08-25", "Deposit", "256.000000", "256.000000"),
        _row("2026-08-26", "Deposit", "1002.000000", "1258.000000"),
        _row("2026-08-28", "Deposit", "500.000000", "1758.000000"),
        _row("2026-09-01", "Buy", "-438.141980", "1319.858020",
             name=EM_NAME, isin=EM, shares="3.615000", price="121.201101"),
        _row("2026-09-01", "Buy", "-1299.834434", "20.023586",
             name=WORLD_NAME, isin=WORLD, shares="2.898000", price="448.528100"),
    )


# ── The happy path, pinned to the real numbers ──────────────────────────────

def test_the_real_shape_parses_and_reconciles():
    report = parse_transaction_report(_realistic())

    assert len(report.rows) == 5
    assert report.first_date == date(2026, 8, 25)
    assert report.last_date == date(2026, 9, 1)
    assert report.final_balance == Decimal("20.023586")
    assert report.warnings == ()

    deposits = [r for r in report.rows if r.kind == DEPOSIT]
    assert sum(r.cash_flow for r in deposits) == Decimal("1758.000000")

    # The names survive quoting intact — both carry "(CH)" and one has a bare "I".
    assert report.assets == {EM: EM_NAME, WORLD: WORLD_NAME}


def test_a_bom_is_tolerated():
    """finpension writes UTF-8 with a BOM; a caller may or may not have stripped it."""
    assert len(parse_transaction_report("﻿" + _realistic()).rows) == 5


def test_cost_basis_comes_from_cash_flow_not_shares_times_price():
    """
    The measured detail. finpension rounds the cash flow to 6dp, so the product does
    not reproduce it, and recomputing accumulates drift into the derived balance until
    the oracle fires on our own arithmetic rather than on a real defect.
    """
    buy = next(r for r in parse_transaction_report(_realistic()).rows if r.isin == EM)

    assert buy.shares * buy.price_chf == Decimal("438.141980115000")
    assert buy.cash_flow == Decimal("-438.141980")
    assert buy.shares * buy.price_chf != -buy.cash_flow  # they genuinely differ


def test_the_direction_of_a_trade_comes_from_the_sign_of_the_cash_flow():
    """
    Which is why `Portfolio Transaction` needs no special case: one category covers a
    rebalance in both directions, and the sign already says which.
    """
    report = parse_transaction_report(_file(
        _row("2026-08-25", "Deposit", "1000.000000", "1000.000000"),
        _row("2026-08-26", "Portfolio Transaction", "-400.000000", "600.000000",
             isin=EM, shares="4.000000", price="100.000000"),
        _row("2026-08-27", "Portfolio Transaction", "150.000000", "750.000000",
             isin=EM, shares="1.500000", price="100.000000"),
    ))
    buy, sell = report.rows[1], report.rows[2]
    assert buy.is_buy and not buy.is_sell
    assert sell.is_sell and not sell.is_buy


# ── The two refusals ────────────────────────────────────────────────────────

def test_an_unknown_category_refuses_the_whole_file():
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report(_file(
            _row("2026-08-25", "Deposit", "1000.000000", "1000.000000"),
            _row("2026-08-26", "Rebalancing Levy", "-5.000000", "995.000000"),
        ))
    message = str(exc.value)
    assert "Rebalancing Levy" in message
    assert "CATEGORY_KINDS" in message


def test_the_oracle_catches_a_row_booked_for_the_wrong_amount():
    """
    The structural guard. A row whose cash flow we would apply differently from what
    finpension applied breaks the replay immediately, at the row that did it.
    """
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report(_file(
            _row("2026-08-25", "Deposit", "256.000000", "256.000000"),
            # Balance says 1258 but the stated flow only accounts for 1000.
            _row("2026-08-26", "Deposit", "1000.000000", "1258.000000"),
        ))
    assert "line 3" in str(exc.value)
    assert "off by" in str(exc.value)


def test_the_oracle_catches_a_missing_row():
    """
    Deleting a row leaves the survivors internally consistent with each other and
    inconsistent with the Balance column — which is exactly the failure a final-only
    check would still catch, but a per-row one localises.
    """
    rows = _realistic().strip().split("\n")
    del rows[2]  # drop the second deposit
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report("\n".join(rows) + "\n")
    assert "off by" in str(exc.value)


def test_the_oracle_catches_two_errors_that_cancel():
    """
    The reason the check is per row rather than only on the last one. Both flows are
    wrong by 100 in opposite directions, so the final balance is right and every
    intermediate one is not.
    """
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report(_file(
            _row("2026-08-25", "Deposit", "356.000000", "256.000000"),
            _row("2026-08-26", "Deposit", "902.000000", "1258.000000"),
        ))
    assert "line 2" in str(exc.value)  # caught at the first, not smuggled to the end


def test_a_partial_export_refuses_because_the_replay_starts_from_zero():
    """
    A 3a account starts at zero, so a full history always replays from zero. A range
    export does not — and it must refuse, because the importer replaces wholesale and
    would otherwise delete the history the file does not cover.
    """
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report(_file(
            _row("2026-09-01", "Deposit", "500.000000", "5500.000000"),
        ))
    assert "partial range" in str(exc.value)


# ── Structural refusals ─────────────────────────────────────────────────────

def test_a_changed_header_refuses_rather_than_being_guessed_at():
    changed = HEADER.replace("Asset Price in CHF", "Asset Price")
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report(changed + "\n")
    assert "header" in str(exc.value).lower()


def test_rows_out_of_date_order_refuse():
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report(_file(
            _row("2026-08-26", "Deposit", "1000.000000", "1000.000000"),
            _row("2026-08-25", "Deposit", "500.000000", "1500.000000"),
        ))
    assert "date order" in str(exc.value)


def test_an_empty_or_headerless_file_refuses():
    with pytest.raises(FinpensionParseError):
        parse_transaction_report("")
    with pytest.raises(FinpensionParseError):
        parse_transaction_report(HEADER + "\n")


@pytest.mark.parametrize("category,cash,extra,expected", [
    ("Deposit", "-100.000000", {}, "not money arriving"),
    ("Transfer vested benefits", "-100.000000", {}, "not money arriving"),
    ("Flat-rate administrative fee", "100.000000", {}, "adds cash"),
    ("Buy", "0.000000", {"isin": EM, "shares": "1.000000", "price": "1.000000"},
     "undecidable"),
    ("Buy", "-100.000000", {"shares": "1.000000", "price": "100.000000"}, "no ISIN"),
    ("Buy", "-100.000000", {"isin": EM, "price": "100.000000"}, "Number of Shares"),
    ("Sell", "100.000000", {"isin": EM, "shares": "-1.000000", "price": "100.000000"},
     "positive quantity"),
])
def test_a_row_that_contradicts_its_own_category_refuses(category, cash, extra, expected):
    """
    Each of these is a file finpension would never write, which is the point: they are
    the shapes a wrong booking rule downstream would need in order to look plausible.
    """
    with pytest.raises(FinpensionParseError) as exc:
        parse_transaction_report(_file(
            _row("2026-08-25", "Deposit", "10000.000000", "10000.000000"),
            _row("2026-08-26", category, cash, "0.000000", **extra),
        ))
    assert expected in str(exc.value)


# ── The vocabulary ──────────────────────────────────────────────────────────

def test_a_liquidation_distribution_is_booked_and_warned_about_rather_than_refused():
    """
    Never yet seen on this account, and deliberately not a refusal.

    The export is full history, so refusing would block **every** future upload
    permanently the moment such a row appeared — a worse failure than an approximation
    that declares itself. The cash is exact; only the position side is uncertain.
    """
    report = parse_transaction_report(_file(
        _row("2026-08-25", "Deposit", "1000.000000", "1000.000000"),
        _row("2026-08-26", "Liquidation distribution", "42.500000", "1042.500000",
             name=EM_NAME, isin=EM),
    ))
    assert report.rows[1].kind == LIQUIDATION
    assert len(report.warnings) == 1
    assert EM in report.warnings[0]
    assert "check the position" in report.warnings[0]


@pytest.mark.parametrize("category", sorted(CATEGORY_KINDS))
def test_every_published_category_parses(category):
    """
    Parametrised over the map itself, so a thirteenth category cannot be added to
    `CATEGORY_KINDS` without a booking that actually works — and cannot be *omitted*
    without the unknown-category refusal firing on a real file.
    """
    kind = CATEGORY_KINDS[category]
    cash, extra = "100.000000", {}
    if kind == TRADE:
        cash, extra = "-100.000000", {"isin": EM, "shares": "1.000000",
                                      "price": "100.000000"}
    elif kind == FEE:
        cash = "-3.900000"
    elif kind == LIQUIDATION:
        extra = {"isin": EM}

    balance = (Decimal("10000.000000") + Decimal(cash)).quantize(Decimal("0.000001"))
    report = parse_transaction_report(_file(
        _row("2026-08-25", "Deposit", "10000.000000", "10000.000000"),
        _row("2026-08-26", category, cash, f"{balance}", **extra),
    ))
    assert report.rows[1].kind == kind


def test_the_vocabulary_covers_every_kind_the_importer_books():
    """
    Both directions. A kind with no category is dead code in the importer; a category
    mapping to a kind nothing books is a row that would move cash and nothing else.
    """
    assert set(CATEGORY_KINDS.values()) == {
        DEPOSIT, TRANSFER_IN, TRADE, FEE, INCOME, LIQUIDATION
    }
