"""
The crypto book's upstream budgets, computed from the constants that set them.

CoinStats charges credits per call against a monthly allowance — 20,000 on the free plan
the key is on (checked 2026-09-28 through the free `/usage/credits`). CoinGecko's Demo
plan allows 10,000 calls a month. The schedule, the page caps and the re-ask bounds all
multiply into what a month costs, and each is a constant a later edit could raise without
noticing what it does to the product. This test does the multiplication, so raising one
of them is a failing test that says by how much, not a surprise at the end of the month.

Since 2026-10-02 the sync no longer pulls CoinStats' history (`/portfolio/chart` +
`/portfolio/pl/history`, 35 credits a day): the book computes its own.
"""
import math

from app.services.coingecko_client import MONTHLY_CALL_ALLOWANCE, SIMPLE_PRICE_CHUNK
from app.services.coinstats_client import (
    CREDIT_COST,
    MAX_COIN_PAGES,
    MAX_TRANSACTION_PAGES,
)
from app.services.crypto_service import CLOSES_RECHECK_HOURS, SNAPSHOT_RUN_COST
from app.services.scheduler_service import CRYPTO_SYNC_HOURS

FREE_PLAN_CREDITS = 20_000
DAYS = 31
# A generous size for the book the CoinGecko budget is sized against.
HELD_COINS = 40


def test_the_derived_costs_follow_the_documented_endpoint_prices():
    assert SNAPSHOT_RUN_COST == CREDIT_COST["value"] + CREDIT_COST["coins"]
    assert CREDIT_COST["usage"] == 0, "the balance check must stay free"


def test_a_normal_month_of_scheduled_syncs_leaves_most_of_the_free_plan():
    """One coin page per run: the case the plan was sized for, which leaves room for
    manual syncs and the one-off rebuild."""
    month = DAYS * len(CRYPTO_SYNC_HOURS) * SNAPSHOT_RUN_COST
    assert month <= FREE_PLAN_CREDITS * 0.25, month


def test_the_worst_scheduled_month_still_fits_the_free_plan():
    """Every sync at the page cap: still inside the allowance."""
    worst_run = CREDIT_COST["value"] + MAX_COIN_PAGES * CREDIT_COST["coins"]
    month = DAYS * len(CRYPTO_SYNC_HOURS) * worst_run
    assert month <= FREE_PLAN_CREDITS, month


def test_the_one_off_rebuild_at_its_page_cap_costs_a_small_share_of_the_plan():
    worst = MAX_TRANSACTION_PAGES * CREDIT_COST["transactions"]
    assert worst <= FREE_PLAN_CREDITS * 0.05, worst


def test_a_worst_coingecko_month_fits_the_demo_plan():
    """Every slot: one `/simple/price` call per chunk of held coins; every coin re-asked
    for closes as often as the re-check allows (a coin CoinGecko cannot fill); one
    `/coins/list` a day for the mapping."""
    spot = math.ceil(HELD_COINS / SIMPLE_PRICE_CHUNK)
    closes_per_day = HELD_COINS * min(len(CRYPTO_SYNC_HOURS), 24 // CLOSES_RECHECK_HOURS)
    month = DAYS * (len(CRYPTO_SYNC_HOURS) * spot + closes_per_day + 1)
    assert month <= MONTHLY_CALL_ALLOWANCE, month
