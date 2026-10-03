"""
The crypto book's formula, on invented numbers (docs/crypto.md):

    value(d) = Σ qty(d) × price(d)
    pnl(d)   = Σ qty(d−1) × (price(d) − price(d−1))

Pinned: a quantity change — a buy, a transfer between exchanges — moves the value and
never the P&L; an unknown price makes the day unknown, never a smaller known number; the
earliest basket stands in for every day before it; USDC (and only USDC) falls back to its
1.00 USD peg, flagged as a peg.
"""
from datetime import date, datetime, timezone

from app.services.crypto_book import (
    PRICE_MARKET,
    PRICE_PEG,
    HoldingsTimeline,
    PriceBook,
    compute_series,
    daily_closes,
    needed_price_dates,
    peg_for,
)

D = lambda day: date(2026, 3, day)  # noqa: E731


def _book(prices, mapping=None, symbols=None):
    keyed = {(coin, D(day)): p for coin, days in prices.items() for day, p in days.items()}
    mapping = mapping or {c: c for c in prices}
    return PriceBook(keyed, mapping, symbols or {})


def test_a_transfer_in_changes_the_value_and_not_the_pnl():
    timeline = HoldingsTimeline({D(1): {"bitcoin": 1.0}, D(2): {"bitcoin": 3.0}})
    book = _book({"bitcoin": {1: 100.0, 2: 100.0, 3: 110.0}})
    points = compute_series(timeline, book, D(1), D(3))
    assert [p.value_usd for p in points] == [100.0, 300.0, 330.0]
    # Day 2: two coins arrived at an unchanged price — zero P&L, not +200.
    assert [p.pnl_usd for p in points] == [None, 0.0, 30.0]


def test_a_sale_is_not_a_loss():
    timeline = HoldingsTimeline({D(1): {"bitcoin": 2.0}, D(2): {"bitcoin": 0.5}})
    points = compute_series(timeline, _book({"bitcoin": {1: 100.0, 2: 90.0}}), D(1), D(2))
    # Yesterday's two coins fell by 10 each; selling 1.5 of them is not part of the P&L.
    assert points[1].pnl_usd == -20.0
    assert points[1].value_usd == 45.0


def test_before_the_earliest_set_the_earliest_set_is_used():
    timeline = HoldingsTimeline({D(5): {"solana": 4.0}})
    assert timeline.qty(D(1)) == {"solana": 4.0}
    assert timeline.qty(D(9)) == {"solana": 4.0}


def test_a_coin_without_a_price_is_left_out_and_named():
    timeline = HoldingsTimeline({D(1): {"bitcoin": 1.0, "solana": 2.0}})
    book = _book({"bitcoin": {1: 100.0, 2: 101.0}, "solana": {1: 10.0}})
    points = compute_series(timeline, book, D(1), D(2))
    # SOL has no price on day 2: out of the value, and its move is not P&L.
    assert points[1].value_usd == 101.0 and points[1].pnl_usd == 1.0
    assert points[1].missing == {"solana"}


def test_a_coin_gaining_its_price_is_never_a_gain():
    timeline = HoldingsTimeline({D(1): {"bitcoin": 1.0, "solana": 2.0}})
    book = _book({"bitcoin": {1: 100.0, 2: 100.0, 3: 100.0}, "solana": {2: 10.0, 3: 12.0}})
    points = compute_series(timeline, book, D(1), D(3))
    assert [p.value_usd for p in points] == [100.0, 120.0, 124.0]
    assert [p.pnl_usd for p in points] == [None, 0.0, 4.0]


def test_a_day_with_nothing_priced_is_unknown():
    timeline = HoldingsTimeline({D(1): {"solana": 2.0}})
    points = compute_series(timeline, _book({}), D(1), D(2))
    assert [(p.value_usd, p.pnl_usd) for p in points] == [(None, None), (None, None)]


def test_usdc_without_a_price_is_one_dollar_and_flagged_as_a_peg():
    timeline = HoldingsTimeline({D(1): {"usd-coin": 50.0}})
    book = _book({}, mapping={"usd-coin": "usd-coin"})
    price, source = book.price("usd-coin", D(1))
    assert (price, source) == (1.0, PRICE_PEG)
    points = compute_series(timeline, book, D(1), D(2))
    assert [p.value_usd for p in points] == [50.0, 50.0]
    assert points[0].pegged == {"usd-coin"}


def test_usdc_with_a_market_price_uses_it():
    book = _book({"usd-coin": {1: 0.9998}})
    assert book.price("usd-coin", D(1)) == (0.9998, PRICE_MARKET)


def test_usdc_under_another_identifier_is_pegged_by_its_symbol():
    assert peg_for("sol:EPjFWdd5", "USDC") == 1.0
    book = PriceBook({}, {}, {"sol:EPjFWdd5": "USDC"})
    assert book.price("sol:EPjFWdd5", D(1)) == (1.0, PRICE_PEG)


def test_no_other_coin_is_ever_pegged():
    for coin, symbol in (("tether", "USDT"), ("dai", "DAI"), ("bitcoin", "BTC")):
        assert peg_for(coin, symbol) is None
    book = _book({}, mapping={"tether": "tether"}, symbols={"tether": "USDT"})
    assert book.price("tether", D(1)) == (None, None)


def test_prices_are_needed_on_held_days_and_the_day_after_a_sale():
    timeline = HoldingsTimeline({D(1): {"a": 1.0}, D(3): {"b": 1.0}})
    needed = needed_price_dates(timeline, D(1), D(4))
    # `a` is held on 1-2 and its last move is priced on 3; `b` from 3, plus 2 for that move.
    assert needed["a"] == {D(1), D(2), D(3)}
    assert needed["b"] == {D(2), D(3), D(4)}


def test_a_coingecko_point_at_midnight_closes_the_day_before():
    def ms(*args):
        return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)

    points = [
        (ms(2026, 3, 2, 0, 0), 100.0),    # closes 03-01
        (ms(2026, 3, 2, 13, 0), 104.0),   # an hourly point inside 03-02
        (ms(2026, 3, 2, 23, 0), 105.0),   # 03-02's last point: its close
        (ms(2026, 3, 3, 9, 0), 107.0),    # today: never a close
    ]
    assert daily_closes(points, today=D(3)) == {D(1): 100.0, D(2): 105.0}
