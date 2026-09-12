"""
Inspect and edit the IBKR -> Yahoo ticker mappings.

`ticker_mappings` decides where every price comes from, and it was the last production
table still edited by hand-written SQL over ssh — no validation, no audit trail, and a
typo that mis-prices a position looks exactly like a real price. That is not theoretical:
an auto-discovered `SBI/TSE -> SBI` row pointed a Toronto CAD holding at an unrelated US
fund and carried it 61% high for months, and because tier 1 of the lookup is this table,
the row kept shadowing the (correct) suffix logic even after that logic was fixed.

So this is the third CLI in the same pattern as `ingest_flex_xml` and `import_prices`,
for the same reason: `/api/` is proxied publicly and unauthenticated, and a route that
redirects where prices come from is a far larger surface than a command over ssh.

Touches no network — mappings only decide what a *later* sync will fetch.

    python -m app.cli.manage_mappings list
    python -m app.cli.manage_mappings set 2330 TWSE 2330.TW --notes "TSMC, Taiwan"
    python -m app.cli.manage_mappings disable SBI TSE --purge-prices

`list` puts the security's currency next to the one its Yahoo ticker implies, because a
disagreement between those two columns *is* the bug above. `disable --purge-prices` is
the documented recovery in one step: stop consulting the mapping and drop the prices it
produced, then let a scheduled job refill them (incremental caching fetches only missing
dates, so this costs no ad-hoc Yahoo call).
"""
import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from decimal import Decimal
from app.clock import utcnow
from types import SimpleNamespace
from typing import Optional, Tuple

import yfinance as yf
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.dividend_payment import DividendPayment
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.ticker_mapping import TickerMapping
from app.repositories.dividend_repository import DividendRepository
from app.repositories.market_price_repository import MarketPriceRepository
from app.repositories.sync_run_repository import SyncRunRepository
from app.repositories.ticker_mapping_repository import TickerMappingRepository
from app.models.security import PRICE_SOURCE_MANUAL, PRICE_SOURCE_SIBLING, PRICE_SOURCE_YAHOO
from app.services.finpension_ingest import (
    NAV_TOLERANCE_PCT,
    PRICE_SOURCE_STATEMENT,
    purge_carried_prices,
)
from app.services.market_data_service import MarketDataService
from app.services.yahoo_rate_limit import is_rate_limit

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_mapping"


class MappingError(Exception):
    """A refused edit — reported, never half-applied."""


def implied_currency(service: MarketDataService, yahoo_ticker: str) -> Optional[str]:
    """
    The currency a Yahoo ticker implies on its own, or None if it implies nothing.

    Reuses `MarketDataService._get_currency_from_ticker` with a security that has no
    currency of its own, so the answer comes from the same override table and suffix map
    the fetch path uses and the two cannot drift apart. A bare ticker legitimately
    returns None: `AMZN` says nothing about its currency.
    """
    return service._get_currency_from_ticker(yahoo_ticker, SimpleNamespace(currency=None))


async def _find_security(db, symbol: str, exchange: str) -> Optional[Security]:
    result = await db.execute(
        select(Security).where(Security.symbol == symbol, Security.exchange == exchange)
    )
    return result.scalars().first()


async def cmd_list(db) -> int:
    service = MarketDataService(db)

    mappings = list((await db.execute(
        select(TickerMapping).order_by(TickerMapping.ibkr_symbol, TickerMapping.ibkr_exchange)
    )).scalars().all())

    if not mappings:
        print("No ticker mappings.")
        return 0

    counts = dict((await db.execute(
        select(MarketPrice.security_id, func.count(MarketPrice.id))
        .group_by(MarketPrice.security_id)
    )).all())

    # Newest estimate row per security. A dividend row older than the mapping's
    # own updated_at was fetched under a *different* ticker — the SBI shape — so
    # showing the two side by side puts that where the mapping is read.
    newest_estimate = dict((await db.execute(
        select(DividendPayment.security_id, func.max(DividendPayment.last_computed))
        .where(DividendPayment.source == "yfinance_estimate")
        .group_by(DividendPayment.security_id)
    )).all())

    # sec/tkr side by side is the point: they must agree, or the mapping is pointing at
    # a different instrument than the security it claims to price.
    header = (
        f"{'id':>3}  {'IBKR':<18} {'Yahoo':<12} {'source':<8} "
        f"{'secCcy':<7} {'tkrCcy':<7} {'prices':>7} {'divAge':>7}  active"
    )
    print(header)
    print("-" * len(header))

    disagreements = []
    predating = []
    for m in mappings:
        security = await _find_security(db, m.ibkr_symbol, m.ibkr_exchange)
        sec_ccy = (security.currency if security else None) or "-"
        tkr_ccy = implied_currency(service, m.yahoo_ticker) or "-"
        n = counts.get(security.id, 0) if security else 0

        flag = ""
        if security and tkr_ccy != "-" and sec_ccy != tkr_ccy:
            flag = "  <-- CURRENCY DISAGREEMENT"
            disagreements.append((m, sec_ccy, tkr_ccy))

        div_age = "-"
        newest = newest_estimate.get(security.id) if security else None
        if newest is not None:
            div_age = f"{(utcnow() - newest).days}d"
            if m.updated_at and newest < m.updated_at:
                flag += "  <-- DIVIDENDS PREDATE MAPPING"
                predating.append((m, newest))

        print(
            f"{m.id:>3}  {m.ibkr_symbol + '@' + m.ibkr_exchange:<18} {m.yahoo_ticker:<12} "
            f"{(m.source or ''):<8} {sec_ccy:<7} {tkr_ccy:<7} {n:>7} {div_age:>7}  "
            f"{'yes' if m.is_active else 'NO'}{flag}"
        )

    if predating:
        print(
            f"\n{len(predating)} mapping(s) have dividend estimates older than the "
            f"mapping itself — those rows came from a different ticker and still drive "
            f"the forecast's cadence:"
        )
        for m, newest in predating:
            print(
                f"  {m.ibkr_symbol}@{m.ibkr_exchange}: newest estimate {newest:%Y-%m-%d}, "
                f"mapping updated {m.updated_at:%Y-%m-%d}. Clear with "
                f"`purge_dividend_estimates {m.ibkr_symbol} {m.ibkr_exchange}`"
            )

    if disagreements:
        print(
            f"\n{len(disagreements)} mapping(s) point at a ticker quoted in a different "
            f"currency than the security. That is how SBI@TSE was mispriced — the prices "
            f"are probably from a different instrument:"
        )
        for m, sec_ccy, tkr_ccy in disagreements:
            print(
                f"  {m.ibkr_symbol}@{m.ibkr_exchange} -> {m.yahoo_ticker}: "
                f"security is {sec_ccy}, ticker implies {tkr_ccy}. Fix with "
                f"`disable {m.ibkr_symbol} {m.ibkr_exchange} --purge-prices` then `set ...`"
            )
    else:
        print("\nNo currency disagreements.")
    return 0


#: How far a candidate ticker's close may sit from a NAV the provider itself published
#: on the same day and still be believed to be the same instrument.
#:
#: Two percent, and both bounds are measured rather than picked. **Above**: a fund NAV
#: is struck and published on a different clock from a Morningstar quote, so the two
#: disagree by a few tenths on any given day — `0P0000S0OD.SW` against the finpension
#: statement for 2026-09-01 read 446.87 / 448.32 / 448.75 across the surrounding days
#: against a stated 448.5281, a spread of 0.6%. **Below**: the failure this exists to
#: catch is a *different share class of the same fund*, and the one that was actually
#: proposed sat +49% away (`0P0000S0OE.SW`, the EM fund's older NT tranche against this
#: account's NMT one). Nothing lands between the two, which is what makes the threshold
#: a real separation rather than a guess.
# NAV_TOLERANCE_PCT lives in finpension_ingest since 2026-09-12: the importer's sibling
# tracking check applies the same bound, and two copies of one tolerance is how they drift.

#: How many published NAVs to check against. More is not better: they come
#: from transaction dates, so a handful spans months and a wrong share class
#: fails on the first one.
NAV_SAMPLE = 8


async def _verify_against_published_navs(
    db, service, security, yahoo_ticker: str,
) -> Optional[str]:
    """
    Compare a candidate ticker's closes against NAVs the provider published itself.

    Returns a refusal message, or None when there is nothing to check or the candidate
    agrees. Only runs for a security that carries `finpension_statement` price rows —
    a statement NAV is an *observation by the party that sold you the shares*, which is
    a stronger oracle than anything else in this codebase and the reason a mapping here
    can be verified at all rather than merely eyeballed.

    This is a Yahoo call, and it is allowed under rule 1 for the same reason the rest of
    this CLI is: a human typed the command. It costs one request.
    """
    rows = (await db.execute(
        select(MarketPrice)
        .where(
            MarketPrice.security_id == security.id,
            MarketPrice.source == PRICE_SOURCE_STATEMENT,
        )
        .order_by(MarketPrice.date.desc())
        .limit(NAV_SAMPLE)
    )).scalars().all()
    if not rows:
        return None

    start = min(r.date for r in rows) - timedelta(days=5)
    end = max(r.date for r in rows) + timedelta(days=5)
    try:
        history = await asyncio.to_thread(
            lambda: yf.Ticker(yahoo_ticker).history(
                start=start.isoformat(), end=end.isoformat(), auto_adjust=False
            )
        )
    except Exception as e:
        return (
            f"Could not fetch {yahoo_ticker} to verify it against the "
            f"{len(rows)} published NAV(s) on record ({type(e).__name__}: {e}). "
            f"Refusing rather than saving an unverified mapping onto a security whose "
            f"real prices are already known — that is the one case where guessing has "
            f"no upside."
        )
    if history.empty:
        return (
            f"{yahoo_ticker} returned no prices at all, so it cannot be the instrument "
            f"whose NAVs this security already carries."
        )

    closes = {d.date(): Decimal(str(round(float(row["Close"]), 6)))
              for d, row in history.iterrows()}
    worst = None
    for row in rows:
        # Nearest trading day within a couple of days: the two sources strike on
        # different clocks, and demanding the exact date would refuse a correct ticker
        # over a Swiss holiday.
        nearby = [closes[d] for d in closes if abs((d - row.date).days) <= 2]
        if not nearby:
            continue
        gap = min(abs(c - row.close_price) / row.close_price * 100 for c in nearby)
        if worst is None or gap > worst[0]:
            worst = (gap, row.date, row.close_price, min(
                nearby, key=lambda c: abs(c - row.close_price)))

    if worst is None:
        return (
            f"{yahoo_ticker} has no prices near any of the {len(rows)} published NAV(s) "
            f"on record, so there is nothing to verify it against."
        )
    gap, when, nav, close = worst
    if gap > NAV_TOLERANCE_PCT:
        return (
            f"{yahoo_ticker} disagrees with the provider's own NAV by {gap:.1f}% "
            f"(on {when}: statement {nav}, Yahoo {close}) — refusing. "
            f"A gap this size is a **different share class of the same fund**, not a "
            f"pricing difference. That is the SBI failure: the prices arrive, they are "
            f"just the wrong instrument's, and nothing downstream can see it. Measured "
            f"example: the EM fund's NT tranche quotes ~49% above this account's NMT "
            f"one under a name that matches perfectly."
        )
    print(
        f"Verified against {len(rows)} published NAV(s): worst gap {gap:.2f}% on "
        f"{when} (statement {nav}, Yahoo {close}), within {NAV_TOLERANCE_PCT}%."
    )
    return None


async def _verify_sibling_returns(
    db, security, yahoo_ticker: str,
) -> Optional[str]:
    """
    For `--sibling`: the candidate's *level* is expected to differ (it is another share
    class), so the level check above is the wrong oracle. What must agree is the *move*:
    between two NAVs the provider published, the sibling's closes must have changed by
    the same ratio, within `NAV_TOLERANCE_PCT`. With a single NAV on record there is
    nothing to compare yet — the level is anchored to it, and the importer's tracking
    check applies this same bound to every later NAV an upload brings.

    One Yahoo request, allowed under rule 1 because a human typed the command.
    """
    rows = (await db.execute(
        select(MarketPrice)
        .where(
            MarketPrice.security_id == security.id,
            MarketPrice.source == PRICE_SOURCE_STATEMENT,
        )
        .order_by(MarketPrice.date.asc())
        .limit(NAV_SAMPLE)
    )).scalars().all()
    if not rows:
        return (
            f"{security.symbol}@{security.exchange} has no statement NAV on record to "
            f"anchor a sibling class to. Import the provider's export first."
        )
    if len(rows) < 2:
        print(
            f"One published NAV on record ({rows[0].date}: {rows[0].close_price}); the "
            f"level is anchored to it. Tracking is checked against every later NAV an "
            f"upload brings, at the same {NAV_TOLERANCE_PCT}% bound."
        )
        return None

    start = rows[0].date - timedelta(days=5)
    end = rows[-1].date + timedelta(days=5)
    try:
        history = await asyncio.to_thread(
            lambda: yf.Ticker(yahoo_ticker).history(
                start=start.isoformat(), end=end.isoformat(), auto_adjust=False
            )
        )
    except Exception as e:
        return (
            f"Could not fetch {yahoo_ticker} to check its moves against the "
            f"{len(rows)} published NAVs ({type(e).__name__}: {e}); refusing."
        )
    if history.empty:
        return f"{yahoo_ticker} returned no prices at all; it cannot stand in for anything."
    closes = {d.date(): Decimal(str(round(float(row["Close"]), 6)))
              for d, row in history.iterrows()}

    def nearest(when):
        candidates = [d for d in closes if abs((d - when).days) <= 2]
        return closes[min(candidates, key=lambda d: abs((d - when).days))] if candidates else None

    worst = None
    for older, newer in zip(rows, rows[1:]):
        c_old, c_new = nearest(older.date), nearest(newer.date)
        if c_old is None or c_new is None:
            continue
        nav_ratio = newer.close_price / older.close_price
        close_ratio = c_new / c_old
        gap = abs(close_ratio / nav_ratio - 1) * 100
        if worst is None or gap > worst[0]:
            worst = (gap, older.date, newer.date)
    if worst is None:
        return (
            f"{yahoo_ticker} has no closes near the published NAV dates, so its moves "
            f"cannot be compared with the fund's."
        )
    gap, d0, d1 = worst
    if gap > NAV_TOLERANCE_PCT:
        return (
            f"{yahoo_ticker} moved {gap:.1f}% differently from the fund between {d0} and "
            f"{d1} (provider NAVs against its closes) — refusing. A sibling class must "
            f"track the fund it stands in for to within {NAV_TOLERANCE_PCT}%."
        )
    print(
        f"Sibling moves verified against {len(rows)} published NAVs: worst tracking gap "
        f"{gap:.2f}% ({d0} → {d1}), within {NAV_TOLERANCE_PCT}%."
    )
    return None


async def cmd_set(
    db, symbol: str, exchange: str, yahoo_ticker: str,
    notes: Optional[str], dry_run: bool, skip_nav_check: bool = False,
    sibling: bool = False,
) -> Tuple[int, dict]:
    service = MarketDataService(db)
    security = await _find_security(db, symbol, exchange)
    tkr_ccy = implied_currency(service, yahoo_ticker)

    if security and tkr_ccy and security.currency and tkr_ccy != security.currency:
        raise MappingError(
            f"{yahoo_ticker} is quoted in {tkr_ccy} but {symbol}@{exchange} is a "
            f"{security.currency} security — refusing. A mapping to the wrong listing is "
            f"invisible downstream: the prices arrive, they are just the wrong company's"
        )

    if not security:
        # Legitimate and expected when pinning a mapping ahead of the statement that
        # first carries the security, so this informs rather than blocks.
        print(
            f"Note: no security matches {symbol}@{exchange} yet — the mapping is stored "
            f"and will apply as soon as one does."
        )
    elif not tkr_ccy and service._get_exchange_suffix(security):
        # The SBI shape: a foreign listing pointed at a suffix-less ticker, which on Yahoo
        # readily resolves to an unrelated US listing of the same symbol. Can't be refused
        # (a bare ticker implies nothing, and an operator may know better than the map),
        # but it should never be typed by accident.
        print(
            f"WARNING: {symbol}@{exchange} is a foreign listing (suffix "
            f"'{service._get_exchange_suffix(security)}') but {yahoo_ticker} has no suffix. "
            f"A bare ticker is how SBI@TSE ended up priced off a US fund — double-check "
            f"this is really the same instrument."
        )

    # A security priced from provider statements carries an oracle nothing else in
    # this app has: NAVs published by the party that sold the shares. Check the
    # candidate against them before believing it, because the failure mode here is
    # a different *share class* — a perfect name match at a completely wrong level.
    verified = False
    sibling_ok = False
    if sibling:
        # Another share class of the same fund: its level is *expected* to disagree
        # with the published NAVs (the EM fund's NT tranche sits 49% above the NMT one
        # held), so the level check would refuse the very thing being declared. What
        # has to agree is the move — see _verify_sibling_returns.
        if not security:
            raise MappingError(
                f"--sibling needs {symbol}@{exchange} to exist with statement NAVs on "
                f"record; import the provider's export first."
            )
        if getattr(security, "price_source", None) == PRICE_SOURCE_YAHOO:
            raise MappingError(
                f"{symbol}@{exchange} already prices from Yahoo directly. A sibling class "
                f"is for a tranche Yahoo does not quote; disable that mapping first if you "
                f"really mean to replace a direct quote with a derived one."
            )
        refusal = await _verify_sibling_returns(db, security, yahoo_ticker)
        if refusal:
            raise MappingError(refusal)
        sibling_ok = True
    elif security and not skip_nav_check:
        refusal = await _verify_against_published_navs(
            db, service, security, yahoo_ticker
        )
        if refusal:
            raise MappingError(refusal)
        verified = getattr(security, "price_source", None) == PRICE_SOURCE_MANUAL

    existing = await service.ticker_mapping_repo.get_mapping(symbol, exchange)
    before = existing.yahoo_ticker if existing else None

    # Changing the ticker retires whatever the old one produced. Prices are
    # obvious; dividend estimates are not, and they outlive the mapping silently —
    # that is the SBI failure exactly. Name them here, at the moment they become
    # suspect, because nothing downstream will.
    stale_estimates = 0
    if security and before and before != yahoo_ticker:
        stale_estimates = len(
            await DividendRepository(db).get_estimates_for_security(security.id)
        )
        if stale_estimates:
            print(
                f"WARNING: {stale_estimates} yfinance dividend estimate(s) for "
                f"{symbol}@{exchange} were fetched under {before} and are now suspect. "
                f"They still drive the forecast's cadence. Clear them with "
                f"`python -m app.cli.purge_dividend_estimates {symbol} {exchange}`."
            )

    if dry_run:
        print(
            f"DRY RUN - would {'update' if existing else 'create'} "
            f"{symbol}@{exchange} -> {yahoo_ticker}"
            + (f" (was {before})" if before else "")
        )
        return 0, {}

    await service.ticker_mapping_repo.upsert_mapping(
        ibkr_symbol=symbol, ibkr_exchange=exchange, yahoo_ticker=yahoo_ticker,
        source="manual", notes=notes,
    )
    # Only a *verified* mapping hands the security to the price loop. Saving the row
    # without this leaves it inert -- `sync_securities` skips a manual security -- so
    # the flip is the second half of the same action, and it deliberately happens
    # nowhere else: a mapping typed without the NAV check does not earn it.
    purged = 0
    if verified:
        security.price_source = PRICE_SOURCE_YAHOO
        # The carry was a stand-in for the feed that did not exist, and it is written
        # forward past today — so leaving it would let a weeks-old NAV **shadow** the
        # feed we just pinned, which is the opposite of what pinning it was for.
        purged = await purge_carried_prices(db, security.id)
        print(
            f"{symbol}@{exchange} now prices from Yahoo (was manual). Dropped {purged} "
            f"carried row(s); its published NAVs stay on record and a Yahoo close "
            f"supersedes them per date."
        )
    elif sibling_ok:
        security.price_source = PRICE_SOURCE_SIBLING
        # Same reason as above: a carry written forward past today would shadow the
        # derived rows the next sync writes.
        purged = await purge_carried_prices(db, security.id)
        print(
            f"{symbol}@{exchange} now prices from sibling class {yahoo_ticker} (was "
            f"{getattr(security, 'price_source', None) and 'manual'}): the newest "
            f"statement NAV anchors the level and the sibling's closes supply each day's "
            f"move. Dropped {purged} carried row(s); the next market-data sync derives the "
            f"prices, and every upload re-anchors them and checks the tracking."
        )
    await db.commit()
    print(
        f"{'Updated' if existing else 'Created'} {symbol}@{exchange} -> {yahoo_ticker}"
        + (f" (was {before})" if before and before != yahoo_ticker else "")
    )
    return 0, {
        "action": "set", "symbol": symbol, "exchange": exchange,
        "yahoo_ticker": yahoo_ticker, "previous": before,
        "price_source_flipped_to_yahoo": verified,
        "price_source_set_to_sibling": sibling_ok,
        "carried_rows_purged": purged,
    }


async def cmd_disable(
    db, symbol: str, exchange: str, purge_prices: bool, dry_run: bool,
) -> Tuple[int, dict]:
    repo = TickerMappingRepository(db)
    mapping = await repo.get_mapping(symbol, exchange)
    if not mapping:
        raise MappingError(f"No active mapping for {symbol}@{exchange}")

    security = await _find_security(db, symbol, exchange)
    price_count = 0
    estimate_count = 0
    if purge_prices:
        if not security:
            raise MappingError(
                f"--purge-prices needs a security to purge, and none matches "
                f"{symbol}@{exchange}"
            )
        price_count = await MarketPriceRepository(db).count_by_security(security.id)
        # Dividend estimates are derived from the SAME Yahoo ticker, so a mapping
        # that produced wrong prices produced wrong dividends too. Purging only
        # prices is what left SBI's two poisoned rows behind to define a gold
        # miner's payout schedule for a month after the mapping was corrected.
        estimate_count = len(
            await DividendRepository(db).get_estimates_for_security(security.id)
        )

    if dry_run:
        print(
            f"DRY RUN - would disable {symbol}@{exchange} -> {mapping.yahoo_ticker}"
            + (f" and delete {price_count} cached price(s) and {estimate_count} "
               f"dividend estimate(s)" if purge_prices else "")
        )
        return 0, {}

    # Soft delete: get_mapping() already filters on is_active, so the row stops being
    # consulted while the record of what was tried survives.
    mapping.is_active = False
    await db.flush()

    deleted = 0
    dividends_deleted = 0
    if purge_prices:
        deleted = await MarketPriceRepository(db).delete_all_for_security(security.id)
        dividends_deleted = await DividendRepository(db).delete_estimates_for_security(
            security.id
        )
    await db.commit()

    print(f"Disabled {symbol}@{exchange} -> {mapping.yahoo_ticker}")
    if purge_prices:
        print(
            f"Deleted {deleted} cached price(s) and {dividends_deleted} dividend "
            f"estimate(s) for security {security.id}. IBKR dividend rows are kept — "
            f"they carry real withholding and no mapping can invalidate them. A "
            f"scheduled job will refill both — do not fetch by hand (CLAUDE.md rule 1)."
        )
    return 0, {
        "action": "disable", "symbol": symbol, "exchange": exchange,
        "yahoo_ticker": mapping.yahoo_ticker, "prices_deleted": deleted,
        "dividend_estimates_deleted": dividends_deleted,
    }


async def run(args) -> int:
    started_at = utcnow()

    async with AsyncSessionLocal() as db:
        try:
            if args.command == "list":
                return await cmd_list(db)

            if args.command == "set":
                code, details = await cmd_set(
                    db, args.symbol, args.exchange, args.yahoo_ticker,
                    args.notes, args.dry_run, args.skip_nav_check, args.sibling,
                )
            else:
                code, details = await cmd_disable(
                    db, args.symbol, args.exchange, args.purge_prices, args.dry_run,
                )

        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, as in the other CLIs: it must never mask the error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED - nothing was changed: {e}", file=sys.stderr)
            return 1

        if details:
            # A mapping change being invisible is why SBI went unnoticed for months, so
            # every edit lands in the same history the syncs report to.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE,
                status="success",
                message=(
                    f"{details['action']} {details['symbol']}@{details['exchange']} "
                    f"-> {details['yahoo_ticker']}"
                ),
                details=details,
                started_at=started_at,
            )
        return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show every mapping with its currency check")

    p_set = sub.add_parser("set", help="Create or update a mapping (source=manual)")
    p_set.add_argument("symbol", help="IBKR symbol, e.g. 2330")
    p_set.add_argument("exchange", help="IBKR exchange, e.g. TWSE")
    p_set.add_argument("yahoo_ticker", help="Yahoo ticker, e.g. 2330.TW")
    p_set.add_argument("--notes", default=None, help="Why this mapping exists")
    p_set.add_argument("--dry-run", action="store_true")
    p_set.add_argument(
        "--skip-nav-check", action="store_true",
        help="Do not verify the ticker against NAVs the provider published. Only "
             "for a security with no such NAVs on record, where the check is a "
             "no-op anyway - it exists so a Yahoo outage cannot block an unrelated "
             "mapping, not so a disagreement can be waved through.",
    )
    p_set.add_argument(
        "--sibling", action="store_true",
        help="The ticker is another share class of the same fund, quoted where this "
             "one is not. Its level is expected to differ; its moves must track the "
             "provider's NAVs. Sets price_source=sibling: the newest statement NAV "
             "anchors the level, the sibling's closes supply each day's move, and "
             "every upload re-anchors and checks the tracking.",
    )

    p_del = sub.add_parser("disable", help="Stop using a mapping (keeps the row)")
    p_del.add_argument("symbol")
    p_del.add_argument("exchange")
    p_del.add_argument(
        "--purge-prices", action="store_true",
        help="Also delete the security's cached prices, which a wrong mapping poisoned",
    )
    p_del.add_argument("--dry-run", action="store_true")

    return parser


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
