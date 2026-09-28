"""
The crypto sync's CoinStats credit budget, computed from the constants that set it.

CoinStats charges credits per call against a monthly allowance — 20,000 on the free plan
the key is on (checked 2026-09-28 through the free `/usage/credits`). The schedule, the
page cap and the history bound all multiply into what a month costs, and each is a
constant a later edit could raise without noticing what it does to the product. This
test does the multiplication, so raising one of them is a failing test that says by how
much, not a surprise on the CoinStats dashboard at the end of the month.
"""
from app.services.coinstats_client import CREDIT_COST, MAX_COIN_PAGES
from app.services.crypto_service import (
    HISTORY_ATTEMPTS_PER_DAY,
    HISTORY_PULL_COST,
    SNAPSHOT_RUN_COST,
)
from app.services.scheduler_service import CRYPTO_SYNC_HOURS

FREE_PLAN_CREDITS = 20_000
DAYS = 31


def test_the_derived_costs_follow_the_documented_endpoint_prices():
    assert SNAPSHOT_RUN_COST == CREDIT_COST["value"] + CREDIT_COST["coins"]
    assert HISTORY_PULL_COST == CREDIT_COST["chart"] + CREDIT_COST["pl_history"]
    assert CREDIT_COST["usage"] == 0, "the balance check must stay free"


def test_a_normal_month_of_scheduled_syncs_leaves_most_of_the_free_plan():
    """One coin page and one history pull a day: the case the plan was sized for, which
    leaves room for manual syncs."""
    month = DAYS * (len(CRYPTO_SYNC_HOURS) * SNAPSHOT_RUN_COST + HISTORY_PULL_COST)
    assert month <= FREE_PLAN_CREDITS * 0.35, month


def test_the_worst_scheduled_month_still_fits_the_free_plan():
    """Every sync at the page cap and every history pull failing and retried: still
    inside the allowance, so no constant here can exhaust it on its own."""
    worst_run = CREDIT_COST["value"] + MAX_COIN_PAGES * CREDIT_COST["coins"]
    month = DAYS * (
        len(CRYPTO_SYNC_HOURS) * worst_run + HISTORY_ATTEMPTS_PER_DAY * HISTORY_PULL_COST
    )
    assert month <= FREE_PLAN_CREDITS, month
