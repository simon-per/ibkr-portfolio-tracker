"""
Replaying a parsed finpension export into the shared tables.

No network. Does not commit — the caller owns the transaction, the same contract
`sync_helper.ingest_flex_statement` has.

**Wholesale replace, not merge.** Every row for this account is deleted and rebuilt
from the file. That is sound only because a finpension export is always full history
from a zero balance, which the parser's Balance oracle has already proved before we
get here — and it is *necessary* because a content-addressed key alone leaves a
restated row's predecessor orphaned beside its replacement. It is the same rule
`etf_baskets` uses ("replaced wholesale, never merged row-by-row") for the same
reason. `replace_basket`'s shrink guard is mirrored here too: a file holding fewer
rows, or ending earlier, than what is stored is refused unless forced.

**Cash, income and fees go to `cash_flows`; nothing goes to `dividend_payments`.**
That is a design decision, not laziness. `CashService.balance_events` counts every
flow type, so the derived balance stays exact; `get_deposits()`'s whitelist excludes
fees and income from contributions with no new logic; and a pillar 3a distribution
never reaching `dividend_payments` means the era splice, the dividend forecast and
the DA-1 reclaim stay IBKR-only **by not having a row to exclude**, rather than by a
filter three readers have to remember. It also happens to be the correct Swiss
treatment: pillar 3a income is not taxable. Both funds held are accumulating tranches,
so the path is dormant on day one — which is when to get it right.

**`realized_pnl` is computed, never left NULL.** `_realized_from_trades` prefers the
`trades` table wholesale the moment any SELL exists and reads `t.realized_pnl or 0`,
so a NULL would report a sale's gain as exactly zero in the blended headline. We
replay the whole ledger, so the FIFO cost is known exactly; it is our own FIFO rather
than a broker's, and the row says so via `close_source`.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.cash_flow import DEPOSIT_WITHDRAW, FEE as FLOW_FEE, INCOME as FLOW_INCOME, TRANSFER_IN as FLOW_TRANSFER_IN
from app.models.market_price import MarketPrice, PRICE_ROW_SOURCE_SIBLING_SCALED
from app.models.security import PRICE_SOURCE_MANUAL, PRICE_SOURCE_SIBLING, Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.models.cash_flow import CashFlow
from app.services.finpension_report import (
    ACCOUNT_CURRENCY,
    DEPOSIT,
    FEE,
    INCOME,
    LIQUIDATION,
    TRADE,
    TRANSFER_IN,
    FinpensionReport,
)

logger = logging.getLogger(__name__)


class FinpensionIngestError(Exception):
    """A refusal at the database phase — nothing is written."""


#: `securities.exchange` for a fund that trades on no exchange. A **non-null sentinel**
#: is load-bearing rather than cosmetic: `_get_yahoo_ticker` returns the bare symbol
#: immediately and never consults `ticker_mappings` when `exchange` is falsy, so a NULL
#: here would make the fund unmappable even after someone pins a real Yahoo ticker.
#: It also keys `UniqueConstraint('isin', 'exchange')`, so the same ISIN could be held
#: at IBKR and in 3a as two rows — the ASML-on-two-exchanges precedent.
FUND_EXCHANGE = "FUND"

#: `market_prices.source` for a NAV finpension actually published on that date, versus
#: one carried forward between statements. Two tags, not one: the column is what every
#: pricing diagnosis reads first, and the staleness detector has to be able to ask for
#: the newest *observed* NAV — otherwise the carry hides the very staleness it bridges.
PRICE_SOURCE_STATEMENT = "finpension_statement"
PRICE_SOURCE_CARRY = "finpension_carry"

#: Price sources that are a *carry* rather than an observation.
#:
#: `find_stale_priced_securities` must exclude these, and the reason is the whole
#: point of the two tags: a carry runs 45 days past the last real NAV, so the newest
#: row of any kind sits in the future and a staleness alarm keyed on `max(date)`
#: could never fire. The carry is exactly what is being bridged.
CARRIED_PRICE_SOURCES = frozenset({PRICE_SOURCE_CARRY})


async def purge_carried_prices(db, security_id: int) -> int:
    """
    Drop a security's carried rows, keeping every observed NAV.

    Called the moment a security gains a real price feed. A carry is a *stand-in*
    for the feed that did not exist, and it is written forward past today — so once
    a feed arrives the carry does not merely become redundant, it **shadows** it:
    the read path asks for today, finds a carried row at a weeks-old NAV, and never
    looks back at the real close a day or two earlier. Measured on production the
    day this shipped: 448.5281 carried against a 451.23 Yahoo close, a 0.6% gap that
    would have widened every day until the carry ran out six weeks later.

    Statement rows stay. Those are observations by the provider, they are what the
    mapping was verified against, and a Yahoo close for the same date supersedes
    them through the ordinary upsert anyway.
    """
    result = await db.execute(
        delete(MarketPrice).where(
            MarketPrice.security_id == security_id,
            MarketPrice.source.in_(CARRIED_PRICE_SOURCES),
        )
    )
    return result.rowcount or 0

#: How far past the last observed NAV a carried price is written.
#:
#: Bounded rather than run to today, deliberately. A six-month-stale upload would
#: otherwise value the fund at a six-month-old NAV for ever, with nothing anywhere
#: saying so. Bounded, it is deterministic (re-importing the same file writes the same
#: rows) and the holding eventually drops out loudly instead of drifting quietly.
CARRY_HORIZON_DAYS = 45

#: `taxlots.close_source` for a lot closed by a finpension sale. A distinct value
#: rather than 'trade' because provenance is the thing this column is for, and the
#: FIFO behind it is ours rather than a broker's.
CLOSE_SOURCE = "finpension"


def _ib_key(account: str, row, seen: Dict[str, int]) -> str:
    """
    Deterministic, content-addressed, and namespaced so it cannot collide with IBKR's.

    The hash covers the running `Balance` as well as the row's own fields, which sounds
    like a bug and is the feature: it makes the key sensitive to the row's *position*
    in the ledger, so a backdated restatement changes every downstream key and becomes
    countable rather than silent. A within-file ordinal disambiguates the only
    collision that remains — two byte-identical zero-cash rows on one date.
    """
    payload = "|".join([
        row.row_date.isoformat(), row.category, row.isin or "",
        str(row.shares or ""), str(row.cash_flow), str(row.balance),
    ])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    key = f"fp:{account}:{digest}"
    seen[key] = seen.get(key, 0) + 1
    return key if seen[key] == 1 else f"{key}-{seen[key]}"


def _business_days(start: date, end: date):
    """Weekdays in ``(start, end]``. Holidays are not knowable per venue here."""
    day = start + timedelta(days=1)
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


#: The `sync_runs.sync_type` the CLI records under. Defined here rather than in the
#: CLI because the shrink guard reads the previous run back.
SYNC_TYPE = "pillar3a_csv"

#: How far a candidate price may sit from a NAV the provider itself published before
#: it is refused as a *different share class*. Both bounds are measured: the correct
#: World ex CH ticker sat 0.6% off at worst, the wrong EM class 49%. Nothing lands
#: between. Shared by `manage_mappings` (verifying a mapping) and the tracking check
#: below (a sibling class drifting away from the fund it stands in for).
NAV_TOLERANCE_PCT = Decimal("2.0")


async def _sibling_tracking_warnings(db, report, security_ids: Dict[str, int]) -> List[str]:
    """
    Does the sibling class still track the fund? Asked on every upload, before the
    replace deletes the evidence.

    A sibling-priced security (`PRICE_SOURCE_SIBLING`) carries rows derived from another
    share class scaled to the *previous* NAV. A new transaction row in this file brings
    a fresh NAV from the provider — the strongest oracle here — and the derived row for
    that day should agree with it to within fee drift. A gap past `NAV_TOLERANCE_PCT`
    means the sibling is not moving like the fund any more (a class was merged,
    re-based, or the mapping points at the wrong one), and the position has been
    mis-valued since the last upload. Reported as a warning, never a refusal: the
    import itself is right, and re-anchoring to the new NAV is exactly the repair.
    """
    warnings: List[str] = []
    for isin, security_id in security_ids.items():
        security = await db.get(Security, security_id)
        if security is None or security.price_source != PRICE_SOURCE_SIBLING:
            continue
        navs = {row.row_date: row.price_chf for row in report.rows
                if row.kind == TRADE and row.isin == isin}
        if not navs:
            continue
        derived = {row.date: row.close_price for row in (await db.execute(
            select(MarketPrice).where(
                MarketPrice.security_id == security_id,
                MarketPrice.source == PRICE_ROW_SOURCE_SIBLING_SCALED,
                MarketPrice.date.in_(list(navs)),
            )
        )).scalars().all()}
        for when, nav in sorted(navs.items()):
            price = derived.get(when)
            if price is None or not nav:
                continue
            gap = abs(price - nav) / nav * 100
            if gap > NAV_TOLERANCE_PCT:
                warnings.append(
                    f"{isin}: the sibling-class price derived for {when} ({price}) is "
                    f"{gap:.1f}% off the NAV the provider published ({nav}). The sibling "
                    f"no longer tracks this fund — check its mapping. Re-anchored to the "
                    f"new NAV now; values between the previous upload and today were off "
                    f"by up to that much."
                )
    return warnings


async def _last_parsed_row_count(db, account: str) -> Optional[int]:
    """
    How many rows the previous successful import of this account *parsed*.

    The shrink guard used to compare the incoming file's row count with the number of
    rows *stored* — and those are different quantities: a row whose CHF→EUR rate was
    missing is parsed, warned about and skipped, so after k such skips a re-export
    missing up to k rows passed the guard and the wholesale replace deleted the
    difference. The skip's own warning ("re-import once the rate is available") is the
    workflow that walked into that hole. `sync_runs.details.rows` holds the parsed
    count of every successful run, so compare parsed with parsed. `None` when no run
    is on record (the first import, or a database that predates the CLI recording
    one), and the caller falls back to the stored count.
    """
    from app.models.sync_run import SyncRun

    rows = (await db.execute(
        select(SyncRun.details)
        .where(SyncRun.sync_type == SYNC_TYPE, SyncRun.status == "success")
        .order_by(SyncRun.finished_at.desc(), SyncRun.id.desc())
    )).scalars().all()
    for details in rows:
        if not isinstance(details, dict) or details.get("account") != account:
            continue
        parsed = details.get("rows")
        return parsed if isinstance(parsed, int) else None
    return None


async def _existing_state(db, account: str) -> Tuple[int, Optional[date], set]:
    """Row count, last trade/flow date, and the ib_keys currently stored."""
    trade_keys = set((await db.execute(
        select(Trade.ib_key).where(Trade.account == account)
    )).scalars().all())
    flow_keys = set((await db.execute(
        select(CashFlow.ib_key).where(CashFlow.account == account)
    )).scalars().all())
    keys = trade_keys | flow_keys

    last_trade = (await db.execute(
        select(Trade.trade_date).where(Trade.account == account)
        .order_by(Trade.trade_date.desc()).limit(1)
    )).scalar_one_or_none()
    last_flow = (await db.execute(
        select(CashFlow.flow_date).where(CashFlow.account == account)
        .order_by(CashFlow.flow_date.desc()).limit(1)
    )).scalar_one_or_none()
    last = max([d for d in (last_trade, last_flow) if d], default=None)
    return len(keys), last, keys


async def ingest_finpension_report(
    db,
    report: FinpensionReport,
    currency_service,
    account: str,
    force: bool = False,
) -> Dict:
    """
    Replace this account's ledger with the report, and return counts + warnings.

    Does not commit. Raises `FinpensionIngestError` before writing anything if the
    file looks like a truncation of what is stored.
    """
    warnings: List[str] = list(report.warnings)

    stored_count, stored_last, stored_keys = await _existing_state(db, account)

    # --- Shrink guard, before any write ------------------------------------------
    # Same shape as the empty-statement wipe guard and `replace_basket`'s row-collapse
    # refusal: a wholesale replace is only safe while the incoming file is at least as
    # complete as what it replaces. Checked up front so a refusal leaves the ledger
    # untouched rather than half-rebuilt. The row baseline is what the previous run
    # *parsed*, not what it stored — see _last_parsed_row_count for why they differ.
    if stored_count and not force:
        previous_rows = await _last_parsed_row_count(db, account)
        baseline = previous_rows if previous_rows is not None else stored_count
        if len(report.rows) < baseline:
            raise FinpensionIngestError(
                f"The file holds {len(report.rows)} rows but the previous import of "
                f"account {account!r} had {baseline}. A finpension export is full "
                f"history, so a shorter one is a truncated download rather than a "
                f"correction, and applying it would delete the difference. Re-export, "
                f"or pass --force if you really mean to replace the ledger with this."
            )
        if stored_last and report.last_date < stored_last:
            raise FinpensionIngestError(
                f"The file ends {report.last_date} but the stored ledger reaches "
                f"{stored_last}. That is an older export; applying it would roll the "
                f"account back. Re-export, or pass --force."
            )

    # --- Securities --------------------------------------------------------------
    from app.repositories.security_repository import SecurityRepository
    security_repo = SecurityRepository(db)

    security_ids: Dict[str, int] = {}
    for isin, name in sorted(report.assets.items()):
        fields = {
            "isin": isin,
            "exchange": FUND_EXCHANGE,
            # No ticker is published, and `ticker_mappings` keys on (symbol, exchange),
            # so the symbol must never change. The ISIN is the only stable identity
            # this instrument has; the readable name is in `description`.
            "symbol": isin,
            "description": name[:200],
            "currency": ACCOUNT_CURRENCY,
            "account": account,
            "asset_type": "ETF",
        }
        existing = await security_repo.get_by_isin_exchange(isin, FUND_EXCHANGE)
        if existing is None:
            # Manual **only on creation**. The dangerous default is the safe one: an
            # un-validated fund is never handed to the variation loop, which is where a
            # bare symbol gets matched to an unrelated listing and auto-saved.
            fields["price_source"] = PRICE_SOURCE_MANUAL
        # ...and never on update, which is the important half. Re-stating it here would
        # make every re-upload silently **un-pin** a Yahoo mapping a human had verified
        # against a published NAV — reverting the fund to statement pricing with nothing
        # saying so, and writing back the carry that shadows the feed.
        security = await security_repo.upsert_by_isin_exchange(fields)
        security_ids[isin] = security.id

    # --- Sibling tracking check, before the replace removes the evidence ------------
    warnings.extend(await _sibling_tracking_warnings(db, report, security_ids))

    # --- Wholesale replace -------------------------------------------------------
    ids = list(security_ids.values())
    if ids:
        await db.execute(delete(TaxLot).where(TaxLot.security_id.in_(ids)))
        # Only our own price rows. A fund later validated onto Yahoo keeps its Yahoo
        # bars, which are better than anything this file can supply. Sibling-derived
        # rows are ours too: they were scaled to the *previous* newest NAV, and the next
        # market-data sync re-derives them from whatever this file makes the newest.
        await db.execute(delete(MarketPrice).where(
            MarketPrice.security_id.in_(ids),
            MarketPrice.source.in_([
                PRICE_SOURCE_STATEMENT, PRICE_SOURCE_CARRY, PRICE_ROW_SOURCE_SIBLING_SCALED,
            ]),
        ))
    await db.execute(delete(Trade).where(Trade.account == account))
    await db.execute(delete(CashFlow).where(CashFlow.account == account))
    await db.flush()

    # --- Replay ------------------------------------------------------------------
    seen_keys: Dict[str, int] = {}
    open_lots: Dict[str, List[dict]] = defaultdict(list)   # FIFO queue per ISIN
    closed_lots: List[dict] = []
    observed_navs: Dict[str, Dict[date, Decimal]] = defaultdict(dict)
    counts = defaultdict(int)
    unconvertible: set = set()

    async def _to_eur(amount: Decimal, on: date) -> Optional[Decimal]:
        if amount == 0:
            return Decimal("0")
        try:
            return await currency_service.convert_to_eur(
                amount=amount, from_currency=ACCOUNT_CURRENCY, target_date=on
            )
        except Exception:
            return None

    for row in report.rows:
        ib_key = _ib_key(account, row, seen_keys)

        if row.kind == TRADE:
            security_id = security_ids[row.isin]
            observed_navs[row.isin][row.row_date] = row.price_chf
            gross = abs(row.cash_flow)

            if row.is_buy:
                cost_eur = await _to_eur(gross, row.row_date)
                if cost_eur is None:
                    unconvertible.add(row.row_date.isoformat())
                    continue
                lot = {
                    "security_id": security_id,
                    "open_date": row.row_date,
                    "quantity": row.shares,
                    # From the cash flow, never shares x price: see the parser.
                    "cost_basis": gross,
                    "price_per_unit": row.price_chf,
                    "currency": ACCOUNT_CURRENCY,
                    "cost_basis_eur": cost_eur,
                    "is_open": True,
                }
                open_lots[row.isin].append(lot)
                counts["lots_opened"] += 1
                realized = None
            else:
                realized, closed = _close_fifo(
                    open_lots[row.isin], row.shares, row.row_date, gross
                )
                closed_lots.extend(closed)
                counts["lots_closed"] += len(closed)

            db.add(Trade(
                ib_key=ib_key,
                # The durable instrument identifier for a ledger that issues none of
                # its own. Only ever matched, never parsed.
                conid=row.isin,
                security_id=security_id,
                symbol=row.isin,
                trade_date=row.row_date,
                buy_sell="BUY" if row.is_buy else "SELL",
                quantity=row.shares if row.is_buy else -row.shares,
                price=row.price_chf,
                proceeds=row.cash_flow,
                commission=Decimal("0"),
                realized_pnl=realized,
                currency=ACCOUNT_CURRENCY,
                asset_category="FUND",
                account=account,
            ))
            counts["trades"] += 1
            continue

        # --- Everything else moves cash and nothing else --------------------------
        flow_type = {
            DEPOSIT: DEPOSIT_WITHDRAW,
            TRANSFER_IN: FLOW_TRANSFER_IN,
            FEE: FLOW_FEE,
            INCOME: FLOW_INCOME,
            LIQUIDATION: FLOW_INCOME,
        }[row.kind]

        amount_eur = await _to_eur(row.cash_flow, row.row_date)
        if amount_eur is None:
            unconvertible.add(row.row_date.isoformat())
            continue

        db.add(CashFlow(
            ib_key=ib_key,
            flow_date=row.row_date,
            flow_type=flow_type,
            amount=row.cash_flow,
            currency=ACCOUNT_CURRENCY,
            amount_eur=amount_eur,
            description=row.category,
            account=account,
        ))
        counts["cash_flows"] += 1
        if flow_type == DEPOSIT_WITHDRAW:
            counts["deposits"] += 1

        if row.kind == LIQUIDATION and row.isin and open_lots.get(row.isin):
            _, closed = _close_fifo(
                open_lots[row.isin],
                sum(lot["quantity"] for lot in open_lots[row.isin]),
                row.row_date,
                abs(row.cash_flow),
            )
            closed_lots.extend(closed)
            counts["lots_closed"] += len(closed)

    if unconvertible:
        warnings.append(
            f"No CHF/EUR rate for {', '.join(sorted(unconvertible))}; those rows were "
            f"skipped rather than stored unconverted. The derived cash balance will be "
            f"short by their amounts until the rate is available and the file re-imported."
        )

    # --- Persist lots -------------------------------------------------------------
    for lot in [l for lots in open_lots.values() for l in lots] + closed_lots:
        db.add(TaxLot(**lot))

    # --- Prices -------------------------------------------------------------------
    # Carry only for securities with no feed of their own. Writing it for one that
    # prices from Yahoo would shadow the feed, for the reason `purge_carried_prices`
    # spells out — and a re-import must not undo what pinning a mapping fixed.
    carry_for = {
        sid for isin, sid in security_ids.items()
        if (await db.get(Security, sid)).price_source == PRICE_SOURCE_MANUAL
    }
    counts["prices_written"] = await _write_prices(
        db, security_ids, observed_navs, report.last_date, carry_for
    )

    restated = len(stored_keys - set(seen_keys)) if stored_keys else 0
    if restated:
        warnings.append(
            f"{restated} previously-stored row(s) are not in this export, so history "
            f"was restated rather than merely extended. Their replacements are in; "
            f"this is worth a look if you did not expect a correction."
        )

    return {
        "account": account,
        "rows": len(report.rows),
        "trades": counts["trades"],
        "cash_flows": counts["cash_flows"],
        "deposits": counts["deposits"],
        "lots_opened": counts["lots_opened"],
        "lots_closed": counts["lots_closed"],
        "securities": len(security_ids),
        "prices_written": counts["prices_written"],
        "rows_restated": restated,
        "period_from": report.first_date.isoformat(),
        "period_to": report.last_date.isoformat(),
        "final_balance_chf": str(report.final_balance),
        "warnings": warnings,
    }


def _close_fifo(
    lots: List[dict], quantity: Decimal, on: date, proceeds: Decimal
) -> Tuple[Decimal, List[dict]]:
    """
    Consume `quantity` from the oldest lots first, returning (realized_pnl, closed).

    Proceeds are apportioned by the share of the quantity each lot supplies, so a sale
    spanning three lots books three closures whose gains sum to the sale's. A partial
    consumption splits the lot pro-rata under its *original* open date, matching what
    `reconcile_taxlots` does for the IBKR side.
    """
    remaining = quantity
    closed: List[dict] = []
    realized = Decimal("0")

    while remaining > 0 and lots:
        lot = lots[0]
        take = min(remaining, lot["quantity"])
        share = take / quantity
        lot_proceeds = proceeds * share
        cost = lot["cost_basis"] * (take / lot["quantity"])

        closed.append({
            **lot,
            "quantity": take,
            "cost_basis": cost,
            "cost_basis_eur": lot["cost_basis_eur"] * (take / lot["quantity"]),
            "is_open": False,
            "close_date": on,
            "close_source": CLOSE_SOURCE,
        })
        realized += lot_proceeds - cost

        if take == lot["quantity"]:
            lots.pop(0)
        else:
            lot["quantity"] -= take
            lot["cost_basis"] -= cost
            lot["cost_basis_eur"] -= lot["cost_basis_eur"] * (take / (lot["quantity"] + take))
        remaining -= take

    return realized, closed


async def _write_prices(
    db, security_ids: Dict[str, int],
    observed: Dict[str, Dict[date, Decimal]], last_date: date,
    carry_for: set,
) -> int:
    """
    Write each observed NAV, then carry it forward over business days.

    The carry is materialised rather than left to `_get_market_price_with_fallback`'s
    14-day walk, because a monthly upload would otherwise leave the fund reading as
    *unpriced* for half of every month — dropping it out of the total and tripping
    `unpriced_holdings`, which is the wrong alarm for "nobody has uploaded lately".

    Materialising it is more honest than the alternative, not less: the read path
    already carries a price forward silently, and giving the carried rows their own
    `source` is what lets the staleness detector ask for the newest *observed* NAV.

    Inserted with ON CONFLICT DO NOTHING on `(security_id, date)`, never a bare add.
    The wholesale replace above deletes only *our* rows, and for a fund pinned to
    Yahoo the market-data sync re-fetches the trailing `PROVISIONAL_PRICE_DAYS` even
    when cached and its ON CONFLICT DO UPDATE rewrites the row's `source` — so a NAV
    struck within three days of an upload was a Yahoo bar by the next upload, and
    re-adding that date raised IntegrityError on flush: an opaque failure in place of
    this module's "refuse whole with a reason". An existing bar wins, which is what the
    comment on the delete already said it wanted. Returns the rows *submitted*, the
    same contract as `MarketPriceRepository.bulk_create` — SQLite's rowcount does not
    tell a DO NOTHING row from an inserted one.
    """
    horizon = last_date + timedelta(days=CARRY_HORIZON_DAYS)
    rows: List[dict] = []

    for isin, by_date in observed.items():
        security_id = security_ids[isin]
        dates = sorted(by_date)
        for i, nav_date in enumerate(dates):
            price = by_date[nav_date]
            rows.append(dict(
                security_id=security_id, date=nav_date, close_price=price,
                currency=ACCOUNT_CURRENCY, source=PRICE_SOURCE_STATEMENT,
            ))

            if security_id not in carry_for:
                continue
            stop = dates[i + 1] if i + 1 < len(dates) else horizon
            for day in _business_days(nav_date, stop):
                if day in by_date:
                    break
                rows.append(dict(
                    security_id=security_id, date=day, close_price=price,
                    currency=ACCOUNT_CURRENCY, source=PRICE_SOURCE_CARRY,
                ))

    written = 0
    # Chunked for SQLite's variable limit, the same 100 `MarketPriceRepository.bulk_create` uses.
    for start in range(0, len(rows), 100):
        chunk = rows[start:start + 100]
        await db.execute(
            sqlite_insert(MarketPrice).values(chunk)
            .on_conflict_do_nothing(index_elements=["security_id", "date"])
        )
        written += len(chunk)

    await db.flush()
    return written
