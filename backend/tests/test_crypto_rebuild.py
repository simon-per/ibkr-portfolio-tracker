"""
The backward rebuild of the crypto book's holdings (`app/cli/crypto_rebuild_holdings.py`),
on invented transactions.

    qty(d) = holdings now − Σ signed legs dated after d (up to the snapshot)

Pinned: a buy, a sale, a transfer and a DCA after the reference day are each undone; a
leg after the snapshot is ignored; a negative quantity and a trusted window that does not
hold one basket are refusals; an unrecognised page shape is a refusal; the probe prints
structure and signs, never an address, a hash or an amount.
"""
from datetime import date, datetime

import pytest

from app.cli.crypto_rebuild_holdings import (
    Leg,
    RebuildRefused,
    check_rebuild,
    describe_page,
    legs_of,
    page_items,
    rebuild,
)

REF = date(2026, 8, 23)
ANCHOR_AT = datetime(2026, 9, 1, 12)


def _leg(day, coin, count, hour=10):
    return Leg(datetime(2026, 8, day, hour), coin, coin.upper(), count)


def test_buys_sales_transfers_and_a_dca_are_undone_day_by_day():
    anchor = {"bitcoin": 0.5, "solana": 10.0, "ethereum": 2.0}
    legs = [
        _leg(26, "bitcoin", 0.1),        # a buy
        _leg(27, "solana", -5.0),        # a sale
        _leg(28, "ethereum", 2.0),       # a transfer in from another wallet
        _leg(29, "bitcoin", 0.01),       # DCA
        _leg(30, "bitcoin", 0.01),       # DCA
    ]
    days = rebuild(anchor, ANCHOR_AT, legs, REF, date(2026, 8, 31))
    assert days[REF] == pytest.approx({"bitcoin": 0.38, "solana": 15.0, "ethereum": 0.0})
    assert days[date(2026, 8, 26)]["bitcoin"] == pytest.approx(0.48)
    assert days[date(2026, 8, 27)]["solana"] == pytest.approx(10.0)
    assert days[date(2026, 8, 31)] == pytest.approx(anchor)
    # The trusted window holds one basket: nothing moved on 23-25.
    check_rebuild(days, set(anchor), {}, REF)


def test_a_leg_after_the_snapshot_is_already_outside_it():
    anchor = {"bitcoin": 1.0}
    late = Leg(datetime(2026, 9, 1, 18), "bitcoin", "BTC", 5.0)
    days = rebuild(anchor, ANCHOR_AT, [late], REF, date(2026, 8, 24))
    assert days[REF]["bitcoin"] == 1.0


def test_a_negative_quantity_refuses_the_whole_rebuild():
    """Undoing a receipt of more than is held means a transaction is missing."""
    days = rebuild({"solana": 1.0}, ANCHOR_AT, [_leg(28, "solana", 4.0)], REF,
                   date(2026, 8, 31))
    with pytest.raises(RebuildRefused, match="negative"):
        check_rebuild(days, {"solana"}, {"solana": "SOL"}, REF)


def test_the_refusal_says_how_far_negative_and_when():
    days = rebuild({"solana": 1.0}, ANCHOR_AT, [_leg(28, "solana", 4.0)], REF,
                   date(2026, 8, 31))
    with pytest.raises(RebuildRefused) as refused:
        check_rebuild(days, {"solana"}, {"solana": "SOL"}, REF)
    assert "SOL down to -3 (2026-08-23 .. 2026-08-27)" in str(refused.value)


def test_bnb_fee_dust_within_the_allowance_is_a_warning_not_a_refusal():
    """Binance's fees in BNB leave a shortfall the transaction list does not carry; up to
    0.05 BNB is accepted (owner's decision), written as 0, and reported."""
    days = rebuild({"bnb": 0.64}, ANCHOR_AT, [_leg(28, "bnb", 0.68)], REF, date(2026, 8, 31))
    warnings = check_rebuild(days, {"bnb"}, {"bnb": "BNB"}, REF)
    assert warnings == [
        "fee dust written as 0: BNB down to -0.04 (2026-08-23 .. 2026-08-27), "
        "within the 0.05 allowance"
    ]


def test_bnb_beyond_the_allowance_and_other_coins_still_refuse():
    days = rebuild({"bnb": 0.64}, ANCHOR_AT, [_leg(28, "bnb", 0.70)], REF, date(2026, 8, 31))
    with pytest.raises(RebuildRefused, match="BNB down to -0.06"):
        check_rebuild(days, {"bnb"}, {"bnb": "BNB"}, REF)
    days = rebuild({"solana": 0.0}, ANCHOR_AT, [_leg(28, "solana", 0.01)], REF,
                   date(2026, 8, 31))
    with pytest.raises(RebuildRefused, match="SOL down to -0.01"):
        check_rebuild(days, {"solana"}, {"solana": "SOL"}, REF)


def test_float_noise_below_a_millionth_is_not_a_refusal():
    days = rebuild({"solana": 1.0}, ANCHOR_AT, [_leg(28, "solana", 1.0000005)], REF,
                   date(2026, 8, 31))
    assert check_rebuild(days, {"solana"}, {"solana": "SOL"}, REF) == []


def test_a_negative_coin_that_would_not_be_written_is_not_a_refusal():
    days = rebuild({"spam": 0.0}, ANCHOR_AT, [_leg(28, "spam", 1e6)], REF, date(2026, 8, 31))
    check_rebuild(days, set(), {}, REF)


def test_a_trusted_window_that_moved_is_refused_with_a_diff():
    days = rebuild({"bitcoin": 1.0}, ANCHOR_AT, [_leg(24, "bitcoin", 0.5)], REF,
                   date(2026, 8, 31))
    with pytest.raises(RebuildRefused, match="trusted window") as refused:
        check_rebuild(days, {"bitcoin"}, {"bitcoin": "BTC"}, REF)
    assert "BTC: 0.5 on 2026-08-23, 1 on 2026-08-24" in str(refused.value)


def test_transfer_legs_are_read_with_their_signs_and_swaps_are_two_legs():
    item = {
        "transactionType": "Swap", "date": "2026-08-28T10:00:00.000Z",
        "coinData": {"identifier": "solana", "symbol": "SOL", "count": 2.0},
        "transfers": [
            {"transferType": "Sent", "items": [
                {"coin": {"identifier": "usd-coin", "symbol": "USDC"}, "count": 300.0}]},
            {"transferType": "Received", "items": [
                {"coin": {"identifier": "solana", "symbol": "SOL"}, "count": 2.0}]},
        ],
    }
    legs = legs_of(item)
    assert [(leg.coin_id, leg.count) for leg in legs] == [("usd-coin", -300.0), ("solana", 2.0)]
    assert legs[0].at == datetime(2026, 8, 28, 10)
    # The coinData reading takes the main coin only.
    assert [(leg.coin_id, leg.count) for leg in legs_of(item, "coindata")] == [("solana", 2.0)]


def test_fees_are_taken_out_only_when_asked():
    item = {"transactionType": "Buy", "date": 1_788_000_000,
            "coinData": {"identifier": "bitcoin", "count": 0.1},
            "fee": {"coin": {"identifier": "bitcoin"}, "count": 0.001}}
    assert [leg.count for leg in legs_of(item, "coindata")] == [0.1]
    assert [leg.count for leg in legs_of(item, "coindata", "subtract")] == [0.1, -0.001]


@pytest.mark.parametrize("item", [
    {"transactionType": "Buy"},                                          # no date
    {"date": "2026-08-28", "coinData": {"symbol": "BTC", "count": 1}},   # no identifier
    {"date": "2026-08-28", "transfers": [{"transferType": "Sent"}]},     # no items list
    "not an object",
])
def test_an_unusable_item_refuses_rather_than_skips(item):
    with pytest.raises(RebuildRefused):
        legs_of(item)


def test_an_unrecognised_page_shape_is_refused():
    assert page_items({"result": [1, 2]}) == [1, 2]
    with pytest.raises(RebuildRefused, match="no item list"):
        page_items({"message": "ok"})
    with pytest.raises(RebuildRefused):
        page_items("<html>")


def test_the_probe_prints_structure_and_signs_never_an_address_hash_or_amount():
    body = {"meta": {"page": 1, "limit": 100}, "result": [{
        "transactionType": "Sent", "date": "2026-08-28T10:00:00Z",
        "hash": {"id": "0xDEADBEEFHASH"}, "note": "rent money",
        "coinData": {"identifier": "bitcoin", "count": -0.123456},
        "transfers": [{"transferType": "Sent", "items": [{
            "coin": {"identifier": "bitcoin"}, "count": -0.123456,
            "to": {"address": "bc1qSECRETADDRESS"},
        }]}],
    }]}
    out = "\n".join(describe_page(body))
    for secret in ("0xDEADBEEFHASH", "rent money", "bc1qSECRETADDRESS", "0.123456"):
        assert secret not in out
    assert "coinData.count=-" in out and "('Sent', '-')" in out
    assert "sum(legs of coinData coin)==coinData.count: True" in out
