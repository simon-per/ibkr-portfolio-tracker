"""
Dividend Service
Fetches dividend ex-dates from yfinance, computes income from tax lots,
converts to EUR, and provides monthly summary data.
"""
import asyncio
import logging
import random
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

import yfinance as yf
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.dividend_repository import DividendRepository
from app.repositories.sync_run_repository import utc_iso
from app.services.currency_service import CurrencyService
from app.services.dividend_forecast import (
    ForecastPayment,
    HistPayment,
    infer_gap_days,
    project_dividends,
)
from app.services.yahoo_eligibility import yahoo_eligible
from app.services.yahoo_rate_limit import is_rate_limit

logger = logging.getLogger(__name__)

# How much dividend history to keep from before a security was bought. The
# forecast needs past ex-dates to infer a cadence, and a newly bought payer has
# none of its own; three years is several cycles of any real schedule while
# still discarding the decades yfinance returns.
PRE_OWNERSHIP_HISTORY_YEARS = 3

# Below this many days held inside the trailing year, a trailing-12M yield divides
# a partial year's income by a full position value and so reads low. 350 rather
# than 365 to absorb a lot opened a few days into the window without flagging
# every long-held position.
TTM_FULL_COVERAGE_DAYS = 350


# How far an ex-date may precede its pay-date for the two sources to be treated as the
# same dividend at the era boundary. Mastercard's 29-day lag is the widest this account
# has — CLAUDE.md cites it as exceeding a whole monthly cycle — so 30 covers every real
# lag here while staying well inside a quarterly payer's 91-day cycle.
#
# **Deliberately not wider**, and the two error directions are not symmetric. Too wide
# swallows a genuinely separate earlier dividend and DELETES real income from a Swiss
# filing aid, which understates taxable income. Too narrow leaves one boundary dividend
# counted twice, which overstates it — visible, already badged `mixed`, and the
# status quo this fix improves on rather than a new fault. For a filing aid the
# understatement is the worse failure, so the window errs toward keeping.
#
# 45 was tried first and was too wide: it matched an estimate exactly 45 days before the
# boundary that was real January income, not the March payment's ex-date.
EX_TO_PAY_MAX_LAG_DAYS = 30

# The exception to that bound: Korean and Taiwanese payers. Their cash routinely lands
# more than a month after the ex-date — SK Hynix went ex 2026-08-28 and paid on 09-30,
# 33 days — so under 30 the IBKR payment never paired with its estimate: the pending
# row never cleared beside the cash that had arrived, and no lag was ever measured.
# Keyed by the PAYMENT's currency, which every row carries, so the matcher decides it
# from the rows alone and none of its five readers has to look a security up.
#
# 75 stays under a quarterly cycle (~91 days) and the match is one-to-one, nearest
# first, so a quarterly payer's previous dividend is never swallowed. The 45-day
# failure above was a USD/EUR payer, which keeps 30.
SLOW_PAYER_CURRENCIES = frozenset({"KRW", "TWD"})
SLOW_PAYER_MAX_LAG_DAYS = 75


def max_pay_lag_days(currency: Optional[str]) -> int:
    """How far after its ex-date a dividend paid in ``currency`` may land and still be
    the same dividend. The one rule every ex→pay pairing reads."""
    if currency and currency.upper() in SLOW_PAYER_CURRENCIES:
        return SLOW_PAYER_MAX_LAG_DAYS
    return EX_TO_PAY_MAX_LAG_DAYS

# How long a dividend that has gone ex but whose cash has not arrived stays on the
# calendar as `pending`. Deliberately wider than EX_TO_PAY_MAX_LAG_DAYS, because the two
# answer different questions: that one decides whether two ROWS are the same dividend
# (too wide deletes real income, so it errs narrow), this one decides how long to keep
# saying "still expected". Korean and Taiwanese payers routinely pay more than a month
# after the ex-date, so 30 would drop exactly the payments that need the longest patience.
#
# Bounded rather than open-ended: a payment IBKR reclassifies or books under another
# instrument never arrives, and an entry that sits on the calendar for ever is how a
# reader learns to stop reading it — the same reason the stale-basket banner had to
# become a refresh.
PENDING_MAX_AGE_DAYS = 90

# How close an accrual must sit to an inferred payment for the two to be the same
# dividend. An accrual is authoritative and supersedes the inference; this is only the
# question of WHICH inferred payment it supersedes. Half a monthly cycle, so a monthly
# payer's genuinely separate next payment is never swallowed.
ACCRUAL_MATCH_DAYS = 15


def match_estimates_to_ibkr(
    estimates: Iterable,
    ibkr_rows: Iterable,
    *,
    max_lag_days: Optional[int] = None,
) -> List[Tuple[Any, Any]]:
    """
    Pair each IBKR payment with the estimate that records the SAME dividend.

    The two sources file one payment under different dates — yfinance under its ex-date,
    IBKR under its pay-date, weeks apart — so identifying the pair is the only way to
    tell a duplicate from a genuinely earlier dividend. Matched per security,
    nearest-first, one-to-one, and bounded to the half-open window
    ``[pay - max_lag_days, pay)``.

    **Never by amount**: one side is gross and the other net, so equal amounts are
    exactly what cannot be relied on. **One-to-one** is what makes a window wider than a
    monthly cycle safe — each IBKR payment consumes at most one estimate, so a monthly
    payer's earlier estimates survive instead of being swallowed by the same window.

    Two callers ask two different questions of the same pairing, which is why this is a
    function and not a line inside one of them: `_splice_by_era` wants the estimates to
    DROP, and `_measured_pay_lags` wants the ex→pay distances to KEEP. A second
    implementation of this matching is the failure mode this codebase keeps hitting.

    ``max_lag_days`` is a parameter for the same reason. The splice must err narrow —
    too wide deletes real income from a filing aid — while a mis-measured lag only
    mis-dates a projection, so the two may legitimately diverge later. Left as None it
    is decided per IBKR row by `max_pay_lag_days` from the payment's currency.

    Returns ``[(estimate, ibkr_row), ...]`` in IBKR pay-date order.
    """
    consumed: set = set()
    pairs: List[Tuple[Any, Any]] = []
    candidates = sorted(
        estimates,
        key=lambda p: (p.ex_date or p.pay_date),
        reverse=True,  # nearest to the pay date first
    )
    for row in sorted(ibkr_rows, key=lambda p: (p.pay_date or p.ex_date)):
        pay = row.pay_date or row.ex_date
        window = (max_lag_days if max_lag_days is not None
                  else max_pay_lag_days(getattr(row, "currency", None)))
        earliest = pay - timedelta(days=window)
        for est in candidates:
            if id(est) in consumed or est.security_id != row.security_id:
                continue
            ex = est.ex_date or est.pay_date
            if earliest <= ex < pay:
                consumed.add(id(est))
                pairs.append((est, row))
                break
    return pairs


# What IBKR writes on a dividend cash line, e.g.
#   "VT(US9220427424) CASH DIVIDEND USD 0.4084 PER SHARE (Ordinary Dividend)"
#   "000660.KS(KR7000660001) CASH DIVIDEND KRW 375 PER SHARE (Ordinary Dividend)"
# Read off a real statement on 2026-10-04. The withholding line repeats the rate with
# "- US TAX" instead of the kind, and is never parsed for either.
_IBKR_PER_SHARE_RE = re.compile(
    r"\b([A-Z]{3})\s+([0-9]+(?:\.[0-9]+)?)\s+PER\s+SHARE\b", re.IGNORECASE
)
_IBKR_KIND_RE = re.compile(r"\(([^()]*)\)\s*$")

# The kinds a forecast must never repeat. A return of capital is left regular on
# purpose: funds pay it inside their ordinary schedule.
SPECIAL_DIVIDEND_KINDS = frozenset({"special", "bonus"})


def parse_ibkr_dividend_description(
    description: Optional[str],
) -> Tuple[Optional[str], Optional[Decimal], Optional[str]]:
    """
    ``(currency, gross rate per share, kind)`` from an IBKR dividend cash line.

    Any part that is not there is None — the cash amounts are real either way, so an
    unparseable description costs only the per-share figure, never the row.
    """
    if not description:
        return None, None, None
    currency = rate = kind = None
    m = _IBKR_PER_SHARE_RE.search(description)
    if m:
        currency, rate = m.group(1).upper(), Decimal(m.group(2))
    k = _IBKR_KIND_RE.search(description)
    if k:
        text = k.group(1).lower()
        if "ordinary" in text:
            kind = "ordinary"
        elif "special" in text:
            kind = "special"
        elif "bonus" in text:
            kind = "bonus"
        elif "return of capital" in text:
            kind = "return_of_capital"
        else:
            kind = "other"
    return currency, rate, kind


def _summary_source(payments, ibkr_from) -> str:
    """
    Which provenance the summary's figures actually carry: 'ibkr', 'mixed' or
    'yfinance_estimate'.

    Same three-way flag the tax report uses, and for the same reason: the era
    splice keeps estimated months before the first IBKR payment, so once the
    ledger starts, the card's total is IBKR actuals *plus* the estimates ahead of
    it. Reporting a flat 'ibkr' claims real withholding for a period with none.
    """
    if not ibkr_from:
        return "yfinance_estimate"
    sources = {p.source for p in payments}
    return "ibkr" if sources <= {"ibkr"} else "mixed"


def _estimated_net_from_gross(
    gross: Optional[Decimal], net_factor: Decimal
) -> Optional[Decimal]:
    """Apply the shared forecast assumption without rounding or mutating history."""
    return gross * net_factor if gross is not None else None


def _forward_basis(total: Decimal, gross_estimate: Decimal) -> str:
    """
    Provenance of the forward yield's net numerator.

    'net' uses broker-reported net, while 'gross_estimate' uses estimated net derived
    from gross with the configured forecast net factor. 'mixed' contains both. Keep
    these wire values for compatibility; the factor is an assumption, not measured tax.
    """
    if gross_estimate <= 0:
        return "net"
    return "gross_estimate" if gross_estimate >= total else "mixed"


class DividendService:
    """Service for fetching and computing dividend income."""

    # Latched the first time Yahoo answers with a rate limit, mirroring
    # `MarketDataService.rate_limited`. On the class as well as the instance so a
    # service built through `__new__` in a test can still read it.
    rate_limited = False

    def __init__(self, db: AsyncSession):
        self.db = db
        self.rate_limited = False
        self.repo = DividendRepository(db)
        self.currency_service = CurrencyService(db)

    async def _get_yahoo_ticker(self, security: Security) -> str:
        """Resolve Yahoo ticker for a security (reuses MarketDataService logic)."""
        from app.services.market_data_service import MarketDataService
        market_service = MarketDataService(self.db)
        return await market_service._get_yahoo_ticker(security)

    async def _first_lot_dates(self) -> Dict[int, date]:
        """Earliest lot open_date per security — the point a dividend can first be earned."""
        rows = await self.db.execute(
            select(TaxLot.security_id, func.min(TaxLot.open_date)).group_by(TaxLot.security_id)
        )
        return {sid: first for sid, first in rows.all() if first}

    async def sync_dividend_data(self) -> Dict:
        """Fetch dividend ex-dates from yfinance for all securities."""
        # yahoo_eligible(). The breakdown reader below deliberately does not filter:
        # it must name every holding, including ones Yahoo cannot price.
        result = await self.db.execute(select(Security).where(yahoo_eligible()))
        securities = list(result.scalars().all())

        if not securities:
            return {'securities_processed': 0, 'dividends_added': 0, 'errors': 0,
                    'message': 'No securities found'}

        logger.info(f"Syncing dividends for {len(securities)} securities")

        # yfinance returns a security's ENTIRE dividend history — Coca-Cola since the
        # 1960s — and every ex-date before we owned a share earns nothing, so
        # compute_dividend_income just writes a zero row to mark it processed. On this
        # account that was 1355 of 1446 rows, reaching back to 1985: pure noise that
        # every reader then has to filter (and one that already caused a bug when a
        # filter was relaxed).
        #
        # But it cannot be cut at the purchase date either: the forecast infers a
        # payout cadence from past ex-dates, so a security bought last month would
        # have nothing to infer from and would be dropped from the forecast — which
        # is exactly what happened to TSMC, Samsung and the SOXQ ETF. Keep a few
        # years before ownership: enough to establish a schedule, not decades of it.
        first_lot = await self._first_lot_dates()
        history_lookback = timedelta(days=365 * PRE_OWNERSHIP_HISTORY_YEARS)

        dividends_added = 0
        errors = 0
        skipped = 0
        pre_ownership_skipped = 0
        # Securities this pass actually asked Yahoo about. `len(securities) - skipped`
        # was reported instead, which on a pass abandoned at 5 of 40 claimed 40 — a
        # partial import indistinguishable from a complete one.
        processed = 0

        pending_ids = [s.id for s in securities]

        for i, security_id in enumerate(pending_ids, 1):
            try:
                # Reloaded by id each iteration rather than held across the loop.
                # `db.rollback()` in the handler below expires EVERY object in the
                # session, so the next iteration's attribute read became a lazy
                # refresh — and in async SQLAlchemy an expired-attribute load outside
                # an await raises MissingGreenlet. `db.get` is awaited and serves the
                # identity map on the happy path, so this costs nothing normally.
                security = await self.db.get(Security, security_id)
                if security is None:
                    continue
                # Staleness check: skip if we fetched dividends < 7 days ago
                last_fetch = await self.repo.get_last_fetch_time(security.id)
                if last_fetch and (utcnow() - last_fetch) < timedelta(days=7):
                    skipped += 1
                    continue

                yahoo_ticker = await self._get_yahoo_ticker(security)
                processed += 1
                logger.info(f"[{i}/{len(pending_ids)}] Fetching dividends for {security.symbol} ({yahoo_ticker})")

                # Rate limit before API call
                await asyncio.sleep(random.uniform(1.0, 2.0))

                # Fetch dividends in a thread
                def _fetch(ticker=yahoo_ticker):
                    return yf.Ticker(ticker).dividends

                dividends_series = await asyncio.to_thread(_fetch)

                if dividends_series is None or dividends_series.empty:
                    logger.info(f"No dividends found for {security.symbol}")
                    continue

                # No lots at all yet (a security whose statement arrived before any
                # purchase) → keep everything rather than guess a cutoff.
                owned_from = first_lot.get(security.id)
                keep_from = owned_from - history_lookback if owned_from else None

                for dt_index, amount in dividends_series.items():
                    ex_date = dt_index.date() if hasattr(dt_index, 'date') else dt_index
                    if keep_from and ex_date < keep_from:
                        pre_ownership_skipped += 1
                        continue
                    await self.repo.upsert_payment({
                        'security_id': security.id,
                        'ex_date': ex_date,
                        'amount_per_share': Decimal(str(amount)),
                        'currency': security.currency,
                        'source': 'yfinance_estimate',
                    })
                    dividends_added += 1

                await self.db.commit()

            except Exception as e:
                # Before the rollback -- it expires the ORM attributes, and a lazy
                # refresh inside an except handler raises MissingGreenlet.
                symbol = security.symbol
                errors += 1
                await self.db.rollback()
                # Rule 1: stop on a rate limit. The 7-day staleness check at the top of
                # this loop means a security this pass never reached simply stays stale
                # and the next run fetches it, so abandoning costs nothing but freshness.
                if is_rate_limit(e):
                    self.rate_limited = True
                    logger.warning(
                        f"Yahoo rate limit at {symbol} "
                        f"({i}/{len(pending_ids)}); abandoning the rest of this pass"
                    )
                    break
                logger.error(f"Error fetching dividends for {symbol}: {e}")
                continue

        logger.info(
            f"Dividend sync complete: added={dividends_added}, skipped={skipped}, "
            f"pre_ownership_skipped={pre_ownership_skipped}, errors={errors}, "
            f"rate_limited={self.rate_limited}"
        )
        result = {
            'securities_processed': processed,
            'dividends_added': dividends_added,
            'skipped': skipped,
            'pre_ownership_skipped': pre_ownership_skipped,
            'errors': errors,
            'rate_limited': self.rate_limited,
            'message': f'Synced dividends: {dividends_added} records from {processed} securities',
        }
        if self.rate_limited:
            # The same sentence the fundamentals, ratings and watchlist passes emit, so
            # the scheduler's `warnings[]` says why the run stopped early instead of the
            # run merely looking complete — this was the one Yahoo loop that latched
            # the flag and then reported nothing.
            result['warnings'] = [
                'Yahoo Finance rate limit reached; the rest of this pass was abandoned. '
                'Do not retry manually — the next scheduled run resumes where it stopped.'
            ]
        return result

    async def compute_dividend_income(self) -> Dict:
        """Compute shares held and EUR amounts for all uncomputed dividend payments."""
        uncomputed = await self.repo.get_uncomputed()
        if not uncomputed:
            return {'computed': 0, 'message': 'All dividends already computed'}

        logger.info(f"Computing income for {len(uncomputed)} dividend payments")

        # Pre-load all tax lots
        taxlot_result = await self.db.execute(select(TaxLot))
        all_taxlots = list(taxlot_result.scalars().all())

        # Group tax lots by security_id
        taxlots_by_security: Dict[int, List[TaxLot]] = defaultdict(list)
        for tl in all_taxlots:
            taxlots_by_security[tl.security_id].append(tl)

        computed = 0
        # Rows left uncomputed because no FX rate could be resolved. Reported rather
        # than silent: the alternative used to be storing the foreign amount as EUR.
        fx_skipped = 0
        errors = 0

        for dp in uncomputed:
            try:
                # Authoritative IBKR rows carry gross/withholding/net directly — never
                # recompute them from per-share estimates.
                if dp.source == "ibkr":
                    continue

                # Sum shares held on ex_date: open_date <= ex_date AND (close_date IS NULL OR close_date > ex_date)
                lots = taxlots_by_security.get(dp.security_id, [])
                shares = Decimal("0")
                for lot in lots:
                    if lot.open_date > dp.ex_date:
                        continue
                    if lot.close_date and lot.close_date <= dp.ex_date:
                        continue
                    shares += lot.quantity

                if shares <= 0:
                    # No shares held on ex-date — set to 0 so it's not re-processed
                    dp.shares_held = Decimal("0")
                    dp.gross_amount_eur = Decimal("0")
                    dp.withholding_tax_eur = Decimal("0")
                    dp.net_amount_eur = Decimal("0")
                    dp.last_computed = utcnow()
                    computed += 1
                    continue

                gross_amount = dp.amount_per_share * shares

                # Convert to EUR
                currency = dp.currency or "USD"
                if currency == "EUR":
                    gross_eur = gross_amount
                else:
                    try:
                        fx_rate = await self.currency_service.get_exchange_rate(
                            currency, dp.ex_date
                        )
                        gross_eur = gross_amount * fx_rate
                    except Exception as e:
                        # `gross_eur = gross_amount  # fallback: store unconverted` is
                        # what stood here — the identical defect fixed in
                        # `TaxService._to_eur`, and then in this file's own `_to_eur`,
                        # surviving in the second conversion path a few dozen lines
                        # below both of them. Fixing a helper is not fixing the file:
                        # this code never went through either helper.
                        #
                        # It writes a foreign figure into a column named `_eur`, on an
                        # ingest path, so the wrong number is *persisted* and then read
                        # by the Dividends tab, the forecast, the forward yield and the
                        # tax report's DA-1 income. A TWD payment would sit in
                        # `gross_amount_eur` roughly 35x high with nothing marking it.
                        #
                        # Leaving the row uncomputed is what makes this self-healing:
                        # `shares_held IS NULL` is the "awaiting computation" sentinel
                        # `prune_empty_dividends` already refuses to touch, so the next
                        # pass retries the row once a rate exists. Reachable whenever
                        # `get_exchange_rate` exhausts both providers — TWD is outside
                        # the ECB set entirely, and the er-api fallback refuses any date
                        # older than FALLBACK_MAX_AGE_DAYS.
                        logger.warning(
                            f"No {currency}->EUR rate for {dp.ex_date} ({e}); leaving this "
                            f"dividend uncomputed rather than storing {currency} as EUR"
                        )
                        fx_skipped += 1
                        continue

                dp.shares_held = shares
                dp.gross_amount_eur = gross_eur
                # yfinance estimates carry no withholding info; net == gross.
                dp.withholding_tax_eur = Decimal("0")
                dp.net_amount_eur = gross_eur
                if dp.source is None:
                    dp.source = "yfinance_estimate"
                dp.last_computed = utcnow()
                computed += 1

            except Exception as e:
                logger.error(f"Error computing dividend for security_id={dp.security_id}, ex_date={dp.ex_date}: {e}")
                errors += 1
                continue

        await self.db.commit()
        logger.info(
            f"Dividend computation complete: computed={computed}, errors={errors}, "
            f"fx_skipped={fx_skipped}"
        )
        result = {
            'computed': computed,
            'errors': errors,
            'fx_skipped': fx_skipped,
            'message': f'Computed {computed} dividend payments',
        }
        if fx_skipped:
            result['warnings'] = [
                f'{fx_skipped} dividend(s) left uncomputed: no exchange rate for their '
                f'currency on the ex-date. They are retried automatically once a rate '
                f'exists — see WARM_CURRENCIES if the currency is not an ECB one.'
            ]
        return result

    async def _to_eur(self, amount: Decimal, currency: str, on_date: date) -> Optional[Decimal]:
        """
        Convert to EUR, or return None when no rate can be resolved.

        It used to `return amount  # fallback: store unconverted`, which writes a
        foreign figure into a column named `_eur` — and unlike the identical defect
        fixed in `TaxService._to_eur` on 2026-07-30, this one is on the **ingest**
        path, so the wrong number is *persisted* and then read by the Dividends tab,
        the forecast, and the tax report's DA-1 income. A TWD payment would sit in
        `gross_amount_eur` roughly 35x high with nothing marking it.

        That fix listed the consumers already skipping correctly — sync_helper,
        portfolio_service, benchmark_service — and missed this sibling, which was
        doing exactly what the tax report used to do.

        Skipping the row matches how every other ingest handles an unconvertible
        currency: `reconcile_taxlots` skips the lot into `taxlots_skipped`, and
        cash-flow ingest skips one row rather than failing the sync.
        """
        if not amount:
            return Decimal("0")
        if (currency or "EUR") == "EUR":
            return amount
        try:
            return await self.currency_service.convert_to_eur(
                amount=amount, from_currency=currency, target_date=on_date
            )
        except Exception as e:
            logger.warning(
                f"No {currency}->EUR rate for {on_date} ({e}); skipping the dividend "
                f"rather than storing {currency} as EUR"
            )
            return None

    async def sync_dividends_from_cash_transactions(
        self, cash_txns: List[Dict], conid_to_security_id: Dict[str, int]
    ) -> Dict:
        """
        Record authoritative dividend income from IBKR <CashTransactions>.

        Groups Dividends + Payment-In-Lieu (gross) and Withholding Tax per
        security per pay date, computes net = gross - withholding, converts to EUR
        at the pay date, and upserts with source='ibkr'. Does NOT commit — the
        caller's sync transaction owns the commit. Tolerant of an empty list.
        """
        if not cash_txns:
            return {"ibkr_dividends": 0, "message": "No dividend cash transactions"}

        conid_map = {str(k): v for k, v in conid_to_security_id.items()}
        grouped: Dict = defaultdict(lambda: {
            "gross": Decimal("0"), "wht": Decimal("0"), "currency": None,
            # IBKR's stated gross rate per share, summed per kind, signed by the line —
            # a reversal posts the same description with a negative amount and must
            # cancel its rate rather than double it.
            "rates": defaultdict(Decimal),
            "rate_currencies": set(),
        })

        for ct in cash_txns:
            security_id = conid_map.get(str(ct["conid"]))
            if not security_id:
                continue
            key = (security_id, ct["pay_date"])
            g = grouped[key]
            g["currency"] = g["currency"] or ct.get("currency")
            if ct["type"] in ("DIVIDEND", "PAYMENTINLIEU"):
                g["gross"] += ct["amount"]
                rate_cur, rate, kind = parse_ibkr_dividend_description(ct.get("description"))
                if rate is not None and ct["amount"]:
                    g["rates"][kind] += rate if ct["amount"] > 0 else -rate
                    g["rate_currencies"].add(rate_cur)
            elif ct["type"] == "WHTAX":
                g["wht"] += ct["amount"]  # IBKR reports withholding as a negative amount

        saved = 0
        skipped_currencies: Dict[str, int] = {}
        now = utcnow()
        for (security_id, pay_date), g in grouped.items():
            gross = g["gross"]
            if gross <= 0:
                # Only withholding with no matching dividend (e.g. a reclass) — skip.
                continue
            withholding = -g["wht"]  # make positive
            currency = g["currency"] or "USD"

            gross_eur = await self._to_eur(gross, currency, pay_date)
            wht_eur = await self._to_eur(withholding, currency, pay_date)
            if gross_eur is None or wht_eur is None:
                # No rate for this date. Storing the foreign figure in a column named
                # `_eur` is what this used to do; a missing dividend is recoverable
                # (the statement is re-ingested idempotently once the rate exists),
                # a silently inflated one is not.
                skipped_currencies[currency] = skipped_currencies.get(currency, 0) + 1
                continue
            net_eur = gross_eur - wht_eur

            # One rate per row. An ordinary dividend and a special paid on the same day
            # arrive as two cash lines in one group: the row's rate is the ordinary one,
            # so the forecast repeats the schedule and never the special. A rate in a
            # currency other than the payment's is not comparable, so it is dropped.
            rates = {k: v for k, v in g["rates"].items() if v > 0}
            # The None key is a rate whose line carried no "(… Dividend)" label.
            kind = "ordinary" if "ordinary" in rates else next(iter(rates), None)
            per_share_native = rates.get(kind)
            if g["rate_currencies"] - {currency}:
                per_share_native = None

            await self.repo.upsert_payment({
                "security_id": security_id,
                "ex_date": pay_date,     # unique key; pay date is fine for tax-year bucketing
                "pay_date": pay_date,
                "amount_per_share": None,
                "currency": currency,
                "shares_held": Decimal("0"),  # non-null so compute_dividend_income skips it
                "gross_amount_eur": gross_eur,
                "withholding_tax_eur": wht_eur,
                "net_amount_eur": net_eur,
                "source": "ibkr",
                "per_share_native": per_share_native,
                "dividend_kind": kind,
                "last_computed": now,
            })
            saved += 1

        logger.info(f"Recorded {saved} IBKR dividend payment(s) from cash transactions")

        warnings: List[str] = []
        if skipped_currencies:
            detail = ", ".join(f"{n} in {cur}" for cur, n in sorted(skipped_currencies.items()))
            # Rides on a successful sync, so it is invisible unless surfaced —
            # sync_helper hoists it into the run's warnings[].
            warnings.append(
                f"Skipped {sum(skipped_currencies.values())} IBKR dividend(s) with no "
                f"FX rate for their pay date ({detail}). Income is understated until a "
                f"rate exists; re-ingesting the statement is idempotent and will pick "
                f"them up."
            )
            logger.warning(warnings[-1])

        return {
            "ibkr_dividends": saved,
            "dividends_skipped": sum(skipped_currencies.values()),
            "warnings": warnings,
            "message": f"Recorded {saved} IBKR dividend payments",
        }

    async def sync_dividend_accruals(
        self, accruals: List[Dict], conid_to_security_id: Dict[str, int]
    ) -> Dict:
        """
        Record the dividends IBKR has announced and not yet paid.

        Replaces the whole set — see `DividendAccrualRepository.replace_all`. Does NOT
        commit; the caller's sync transaction owns that. Tolerant of an empty list, which
        is the normal state until the ``<OpenDividendAccruals>`` section is enabled in
        the Flex Query, and is also what a statement says when everything has been paid.

        **Refuses whole rather than half-applying.** A row whose currency has no cached
        rate is skipped and counted, never stored unconverted into a column named `_eur`
        — the same rule `sync_dividends_from_cash_transactions` learned. The pay date is
        in the future and has no rate of its own, so the newest cached one is used: a
        payment yet to happen is best sized at today's rate, exactly as the forecast
        sizes a projection.
        """
        from app.repositories.dividend_accrual_repository import (
            DividendAccrualRepository,
        )

        repo = DividendAccrualRepository(self.db)
        if not accruals:
            # Still a replace: an enabled section listing nothing means every accrual has
            # been paid, and leaving the old rows would keep promising money that landed.
            # Harmless when the section is absent, since the table is empty anyway.
            await repo.replace_all([])
            return {"dividend_accruals": 0, "message": "No open dividend accruals"}

        conid_map = {str(k): v for k, v in conid_to_security_id.items()}
        fx = await self._latest_fx_to_eur(
            {a.get("currency") for a in accruals if a.get("currency")}, date.today()
        )

        rows: List[Dict] = []
        seen: set = set()
        skipped_currencies: Dict[str, int] = {}
        unknown_conids = 0
        now = utcnow()
        for a in accruals:
            security_id = conid_map.get(str(a["conid"]))
            if not security_id:
                unknown_conids += 1
                continue
            key = (security_id, a["pay_date"])
            if key in seen:
                continue  # one open accrual per security per pay date
            currency = a.get("currency") or "EUR"
            rate = fx.get(currency)
            if rate is None:
                skipped_currencies[currency] = skipped_currencies.get(currency, 0) + 1
                continue
            gross = (a.get("gross_amount") or Decimal("0")) * rate
            # Stored positive. IBKR sends `tax` POSITIVE on <OpenDividendAccrual> (a real
            # statement, 2026-10-04: TSMC gross 259, tax 54.39, net 204.61), the opposite
            # of the cash ledger's WHTAX lines; this flipped it negative until then. abs()
            # rather than trusting either sign, since the net is what matters downstream.
            wht = abs(a.get("tax") or Decimal("0")) * rate
            net = a.get("net_amount")
            net_eur = net * rate if net is not None else gross - wht
            if net_eur <= 0:
                # A reversal or a zero accrual is not an upcoming payment.
                continue
            seen.add(key)
            rows.append({
                "security_id": security_id,
                "ex_date": a.get("ex_date"),
                "pay_date": a["pay_date"],
                "currency": currency,
                "quantity": a.get("quantity"),
                "gross_amount_eur": gross,
                "withholding_tax_eur": wht,
                "net_amount_eur": net_eur,
                "last_seen_at": now,
            })

        saved = await repo.replace_all(rows)
        logger.info(f"Recorded {saved} open dividend accrual(s) from Flex")

        warnings: List[str] = []
        if skipped_currencies:
            detail = ", ".join(f"{n} in {cur}" for cur, n in sorted(skipped_currencies.items()))
            warnings.append(
                f"Skipped {sum(skipped_currencies.values())} announced dividend(s) with "
                f"no cached FX rate ({detail}). Their expected payment dates fall back "
                f"to a lag measured from history until a rate exists."
            )
            logger.warning(warnings[-1])
        return {
            "dividend_accruals": saved,
            "accruals_skipped": sum(skipped_currencies.values()) + unknown_conids,
            "warnings": warnings,
            "message": f"Recorded {saved} open dividend accruals",
        }

    async def sync_dividend_date_pairs(
        self, pairs: List[Dict], conid_to_security_id: Dict[str, int]
    ) -> Dict:
        """
        Add IBKR's (ex_date, pay_date) pairs to `dividend_date_pairs`. Insert-if-absent,
        never replace: the table is a log that outlives the statement period (see the
        model). Does NOT commit; the sync transaction owns that. An empty list — the
        section not ticked — is a supported no-op.
        """
        from app.models.dividend_date_pair import DividendDatePair

        if not pairs:
            return {"dividend_date_pairs": 0}
        conid_map = {str(k): v for k, v in conid_to_security_id.items()}
        existing = {
            (r.security_id, r.ex_date, r.pay_date)
            for r in (await self.db.execute(select(DividendDatePair))).scalars().all()
        }
        added = 0
        now = utcnow()
        for p in pairs:
            security_id = conid_map.get(str(p["conid"]))
            if not security_id:
                continue
            key = (security_id, p["ex_date"], p["pay_date"])
            if key in existing:
                continue
            existing.add(key)
            self.db.add(DividendDatePair(
                security_id=security_id, ex_date=p["ex_date"], pay_date=p["pay_date"],
                first_seen_at=now,
            ))
            added += 1
        await self.db.flush()
        return {"dividend_date_pairs": added}

    async def _exact_date_pairs(self) -> Dict[int, List[Tuple[date, date]]]:
        """``{security_id: [(ex_date, pay_date), ...]}`` from IBKR's accrual log."""
        from app.models.dividend_date_pair import DividendDatePair

        out: Dict[int, List[Tuple[date, date]]] = defaultdict(list)
        for r in (await self.db.execute(select(DividendDatePair))).scalars().all():
            out[r.security_id].append((r.ex_date, r.pay_date))
        return out

    async def _open_accruals(self) -> Dict[int, List[Dict]]:
        """
        ``{security_id: [{ex_date, pay_date, net_eur, quantity, gross_eur, wht_eur}, ...]}``
        for every open accrual.

        Empty whenever the Flex section is not enabled, which is the supported default —
        the caller then dates its projections from a measured lag instead and labels them
        so. An accrual whose pay date has already passed is kept: it is still open, which
        means IBKR has not paid it, and that is precisely the state the calendar exists
        to show.

        **Unbounded by age, unlike the two inferred tails.** Those expire at
        PENDING_MAX_AGE_DAYS because they are guesses, and a guess nothing confirms has
        to stop claiming. This is not a guess: a row exists here only because the last
        statement listed the dividend as open, `replace_all` having deleted everything
        the statement did not list. Ageing it out would hide a liability IBKR is still
        asserting — and would hide it precisely in the case that most needs seeing, a
        payment overdue by months.
        """
        from app.repositories.dividend_accrual_repository import (
            DividendAccrualRepository,
        )

        rows = await DividendAccrualRepository(self.db).get_open()
        out: Dict[int, List[Dict]] = defaultdict(list)
        for r in rows:
            out[r.security_id].append({
                "ex_date": r.ex_date,
                "pay_date": r.pay_date,
                "net_eur": r.net_amount_eur,
                # What the forecast sizes from (gross / quantity) and the withholding
                # ladder's first rung (tax / gross).
                "quantity": r.quantity,
                "gross_eur": r.gross_amount_eur,
                "wht_eur": r.withholding_tax_eur,
            })
        return out

    async def _latest_fx_to_eur(self, currencies, as_of: date) -> Dict[str, Decimal]:
        """
        Newest cached <currency>→EUR rate on or before ``as_of``, per currency.

        Cache-only on purpose: this endpoint must never reach a provider. A future
        payment is best sized with the latest known rate rather than the one that
        applied when some historical dividend was paid.
        """
        out: Dict[str, Decimal] = {"EUR": Decimal("1")}
        for cur in currencies:
            if cur in out:
                continue
            recent = await self.currency_service._get_most_recent_rate(cur, as_of, "EUR")
            if recent:
                out[cur] = recent[0]
            else:
                logger.info(f"Dividend forecast: no cached {cur}->EUR rate, skipping its per-share amounts")
        return out

    async def ibkr_cash_receipts(self) -> List[tuple]:
        """
        Dividend cash that actually landed in the IBKR account: ``(pay_date, net_eur)``.

        **Deliberately not era-spliced, and that is not an oversight.** The splice
        answers "how much dividend income was earned", for which a `yfinance_estimate`
        before the ledger begins is the only evidence there is. This answers a
        different question — "how much cash arrived at *this broker*" — and an estimate
        is evidence of nothing there: it is a guess about a payment made into a
        Trading 212 or Scalable Capital account that IBKR's cash balance never saw.
        Splicing here would credit the account with money it was never paid, which is
        the one direction a balance must never err in.

        It lives beside `_net_eur` rather than in `CashService` because that is where
        every other rule about these rows lives, and a reader that reaches into the
        columns itself is what `test_era_splice_boundary` exists to catch.

        Net, not gross: withholding is deducted before the cash reaches the account.
        `_is_income` drops the zero rows yfinance's pre-ownership history writes — they
        carry no cash by definition, and IBKR rows are never zero-valued anyway.
        """
        payments = await DividendRepository(self.db).get_ibkr_payments()
        out = []
        for p in payments:
            if not self._is_income(p):
                continue
            when = p.pay_date or p.ex_date
            if when is None:
                continue
            out.append((when, self._net_eur(p)))
        return out

    @staticmethod
    def _net_eur(p) -> Decimal:
        """
        Net for a payment, falling back to gross when net is NULL.

        Rows predating the withholding-fields migration carry only
        `gross_amount_eur`; treating their net as 0 (or None) would drop real
        income — or crash the arithmetic. get_dividend_summary has always done
        this; every consumer must.
        """
        if p.net_amount_eur is not None:
            return p.net_amount_eur
        return p.gross_amount_eur or Decimal("0")

    @staticmethod
    def _is_income(p) -> bool:
        """
        True when a row represents money actually received.

        yfinance returns a security's ENTIRE dividend history — Coca-Cola pays
        since the 1960s — and compute_dividend_income() writes a zero row for
        every ex-date where no shares were held, deliberately, so it isn't
        reprocessed. Those are bookkeeping, not income: counting them gave the
        summary 439 months back to 1985 of which 419 were empty.
        """
        return ((p.gross_amount_eur or Decimal("0")) > 0
                or (p.net_amount_eur or Decimal("0")) > 0)

    @staticmethod
    def _splice_by_era(payments: List, *, boundary: Optional[date] = None) -> tuple:
        """
        Honest mix of the two sources: yfinance estimates strictly BEFORE the first
        IBKR payment date, authoritative IBKR rows from there on.

        A global "prefer ibkr" switch is wrong in both directions — IBKR rows exist
        only from the date the cash-transaction ledger starts (a YTD Flex Query
        can't reach earlier years), so filtering to ibkr erases all earlier history,
        while keeping estimates inside the IBKR era would double-count the same
        dividend from both sources. Returns (kept_payments, ibkr_from) where
        ibkr_from is None when no IBKR rows exist.

        ``boundary`` overrides the derived era start, for the one caller that windows
        before splicing (``ActivityService._dividends``). It must also widen its fetch
        by ``EX_TO_PAY_MAX_LAG_DAYS`` on both sides, because the duplicate match below
        needs the IBKR row that pairs with a windowed estimate — which can fall outside
        the window even when the estimate does not.
        """
        ibkr_rows = [p for p in payments if p.source == "ibkr"]
        if boundary is None:
            # Derived from the rows given, which is right for every reader that splices
            # the FULL history. A caller that windows first must pass the whole-table
            # boundary instead (`DividendRepository.earliest_ibkr_payment_date`), or a
            # window opening after the era began would treat its own earliest IBKR row
            # as the era start and resurrect superseded estimates.
            if not ibkr_rows:
                return list(payments), None
            boundary = min((p.pay_date or p.ex_date) for p in ibkr_rows)

        kept = [
            p for p in payments
            if p.source == "ibkr" or (p.pay_date or p.ex_date) < boundary
        ]

        # The boundary alone leaks one dividend per security, because the two sources
        # file the SAME payment under different dates: yfinance under its ex-date, IBKR
        # under its pay-date, weeks apart. So the first IBKR payment's own estimate sits
        # *before* the boundary, and the rule above keeps it — beside the IBKR row it
        # duplicates.
        #
        # Seen on production, ASML dual-listed, boundary 2026-02-18:
        #     2026-02-09  yfinance_estimate     2026-02-18  ibkr
        #     2026-02-10  yfinance_estimate     2026-02-18  ibkr
        # Four rows for two dividends, on every reader that splices.
        #
        # Matched per security, nearest-first, one-to-one, and bounded — the rules and
        # the reason they are a shared function live on `match_estimates_to_ibkr`.
        consumed = {
            id(est) for est, _ in match_estimates_to_ibkr(
                [p for p in kept if p.source != "ibkr"], ibkr_rows
            )
        }

        if consumed:
            kept = [p for p in kept if id(p) not in consumed]
        return kept, boundary

    @staticmethod
    def _measured_pay_lags(
        raw_payments: List,
        exact_pairs: Optional[Dict[int, List[Tuple[date, date]]]] = None,
    ) -> Dict[int, Tuple[int, int]]:
        """
        ``{security_id: (median_lag_days, samples)}`` — how long after its ex-date this
        security's dividend actually reaches the account.

        The only pay date this application ever learns comes from IBKR, and the only
        ex-date from yfinance, on two different rows. Pairing them is what turns the two
        halves into one measurement, and `match_estimates_to_ibkr` already knows how.

        Read from the **raw** history rather than the spliced one, and that is the whole
        reason this is measurable: `_splice_by_era` drops post-boundary estimates at READ
        time, so the table still holds an estimate beside every IBKR payment. Measured on
        production 2026-09-19: every IBKR payment on record paired, lags 7–29 days, and
        stable per security to a day or two.

        The median, not the mean — one late settlement should not move a schedule, the
        same argument `dividend_forecast._per_share` makes about a special dividend.

        Absent rather than zero for a security that has never been paid through IBKR: a
        0-day lag is a claim that the cash arrives on the ex-date, and the caller needs to
        know it is falling back to the ex-date rather than being told that is the answer.

        **IBKR's own pairs win** (``exact_pairs``, from `dividend_date_pairs`): for a
        security that has any, the lag is the median of those and the proximity pairing
        is not consulted. Proximity is an inference bounded by a window; these are the
        two dates as IBKR booked them, so they need no window at all.
        """
        exact_pairs = exact_pairs or {}
        estimates = [p for p in raw_payments if p.source != "ibkr"]
        ibkr_rows = [p for p in raw_payments if p.source == "ibkr"]
        lags: Dict[int, List[int]] = defaultdict(list)
        if ibkr_rows:
            for est, row in match_estimates_to_ibkr(estimates, ibkr_rows):
                if row.security_id in exact_pairs:
                    continue
                ex = est.ex_date or est.pay_date
                pay = row.pay_date or row.ex_date
                lags[row.security_id].append((pay - ex).days)
        for sid, pairs in exact_pairs.items():
            lags[sid] = [(pay - ex).days for ex, pay in pairs]
        return {sid: (int(median(v)), len(v)) for sid, v in lags.items() if v}

    @staticmethod
    def _pct(current: Decimal, base: Optional[Decimal]) -> Optional[float]:
        """
        Growth of ``current`` over ``base``, or None when there is nothing to grow
        from.

        A zero base is not a small base — the percentage is undefined, and a
        quarterly payer produces zero months constantly (this account pays nothing
        in April, August or November). Returning a number there would put a
        fabricated figure on screen beside measured ones, so callers get None and
        the UI renders a dash.
        """
        if base is None or base <= 0:
            return None
        return round(float((current - base) / base * 100), 1)

    @staticmethod
    def _shift_month(month_key: str, months_back: int) -> str:
        """'2026-01' shifted back 1 -> '2025-12'. Month keys, not dates."""
        year, month = int(month_key[:4]), int(month_key[5:7])
        total = year * 12 + (month - 1) - months_back
        return f"{total // 12:04d}-{total % 12 + 1:02d}"

    @classmethod
    def _rolling_twelve_months(
        cls,
        *,
        month_actual_sym_all: Dict[str, Dict[str, Decimal]],
        month_forecast_sym_all: Dict[str, Dict[str, Decimal]],
        month_sources_all: Dict[str, set],
        first_income: Optional[date],
        current_month: str,
        horizon_month: str,
        axis_start: Optional[str],
        axis_end: Optional[str],
        windowed: bool,
        include_forecast: bool,
        has_forward_projection: bool,
    ) -> List[Dict]:
        """
        Rolling twelve-month totals, one point per month, stacked by symbol.

        Three rules, each of which would be a wrong number the other way:

        **The whole series is built before any of it is filtered.** A point's
        month-over-month compares against the previous month's window, which on a
        year view is the previous December — a point the reader never sees. Slicing
        first would make January's change depend on the range selected, and the
        figures for one month must be the same whichever range is showing it.

        **A window needs twelve months of history or it does not exist.** Coverage
        starts at the first month that carried income; before that the months are
        unknown, not zero, and a partial sum presented as a year would read as a
        collapse. Nothing is emitted rather than a null the client has to strip —
        which is also what leaves the chart with no empty leading stretch.

        **A measured zero is kept.** A payer that stops really does take its
        rolling total to zero, and that is the one thing this chart exists to show.
        Only absent coverage is dropped.

        Forecast is folded in as though it had been received, so a window reaching
        past today is part measured and part projected. With ``include_forecast``
        false there is no projection to fold, so the series simply stops at the
        last elapsed month rather than publishing windows that are short by
        however much of them has not happened yet.

        **`partial` means the window has not fully elapsed, and nothing else.** It
        is deliberately not "carries projection": a dividend that went ex and has
        not paid puts projection inside a CLOSED window, and dropping that window
        would make twelve months of measured income absent over one unsettled
        payment — the short-sum rule inverted. So the client hides a projection by
        stripping it from the point (`withoutForecast` in `lib/dividendChart.ts`),
        not by dropping the point; only genuinely open windows go.
        """
        if first_income is None or axis_start is None or axis_end is None:
            return []

        coverage_start = cls._shift_month(first_income.strftime("%Y-%m"), -11)
        # Gate on a projection EXISTING, not on the flag asking for one. With the
        # flag set and nothing projected — nothing held, too thin a history, a
        # payer past the stopped guard — a window reaching into the future is all
        # elapsed months and empty ones, so the series would decay month by month
        # to 0.00 and draw a collapse that never happened. That is the shape this
        # service already served once as `next_12m_vs_ttm_pct: -100.0`: a figure
        # manufactured by a flag rather than measured from anything.
        #
        # Portfolio-level, deliberately: asking it per window would punch holes in
        # an annual payer's series, where the window ending in January contains no
        # projection and the one ending in March does.
        #
        # A FORWARD projection, specifically — not merely a non-empty
        # `month_forecast_sym_all`, which now also carries calendar entries whose
        # date has already passed. One overdue payment is not grounds to run the
        # series out to next December over months nothing is expected in, which is
        # the decay this gate exists to prevent.
        projecting = include_forecast and has_forward_projection
        if projecting:
            # The HORIZON, not the last projected payment. Ending at the last
            # payment would put the series' end wherever one payer's final
            # projection happened to fall, so all-time stopped in October while
            # `year=` for the same year ran to December — the same month computing
            # to the same number in one range and not existing in the other.
            # Ending at the horizon makes every range build one identical series
            # that only the slice below differs on.
            #
            # On the all-time view this reaches a year past `months`, which stops
            # at 31 December by design. That is the whole reason this is a separate
            # list: it can have the wider reach without stretching the monthly
            # chart or its forecast total.
            last = max(axis_end, horizon_month)
        else:
            last = min(axis_end, cls._shift_month(current_month, 1))

        points: List[Dict] = []
        prev_total: Optional[Decimal] = None
        prev_forecast: Decimal = Decimal("0")
        prev_sources: set = set()
        mk = coverage_start
        while mk <= last:
            window = [cls._shift_month(mk, back) for back in range(12)]
            actual: Dict[str, Decimal] = defaultdict(Decimal)
            forecast: Dict[str, Decimal] = defaultdict(Decimal)
            sources: set = set()
            for key in window:
                for sym, v in month_actual_sym_all.get(key, {}).items():
                    actual[sym] += v
                for sym, v in month_forecast_sym_all.get(key, {}).items():
                    forecast[sym] += v
                sources |= month_sources_all.get(key, set())

            net = sum(actual.values(), Decimal("0"))
            fc = sum(forecast.values(), Decimal("0"))
            total = net + fc
            points.append({
                "month": mk,
                "actual": {s: round(float(v), 2) for s, v in sorted(actual.items())},
                "forecast": {s: round(float(v), 2) for s, v in sorted(forecast.items())},
                "net_eur": round(float(net), 2),
                "forecast_net_eur": round(float(fc), 2),
                "total_eur": round(float(total), 2),
                "mom_pct": cls._pct(total, prev_total),
                # Either side carrying projection is enough: a measured window
                # compared against a projected one is still a comparison against a
                # guess, and the chip must say so rather than look measured.
                "mom_includes_forecast": fc > 0 or prev_forecast > 0,
                "source": "mixed" if len(sources) > 1 else next(iter(sources), None),
                "mom_crosses_era": (
                    prev_total is not None and len(sources | prev_sources) > 1
                ),
                "partial": mk >= current_month,
            })
            prev_total, prev_forecast, prev_sources = total, fc, sources
            mk = cls._shift_month(mk, -1)

        # Only now, with every comparison already made against its true neighbour,
        # narrow to what the selected range should show.
        if windowed:
            points = [p for p in points if axis_start <= p["month"] <= axis_end]
        return points

    @staticmethod
    def _same_day_last_year(d: date) -> date:
        """
        The same calendar day a year earlier, for like-for-like YTD comparison.
        29 February has no counterpart, so it falls back to the 28th.
        """
        try:
            return d.replace(year=d.year - 1)
        except ValueError:
            return date(d.year - 1, 2, 28)

    @staticmethod
    def _withholding_rates(
        raw_payments: List,
        securities: Dict[int, Security],
        accruals_by_sec: Dict[int, List[Dict]],
        net_factor: Decimal,
    ) -> Dict[int, Tuple[Decimal, str]]:
        """
        ``{security_id: (withholding rate, source)}`` — the share of a gross dividend
        the forecast expects to lose to tax, and where that number came from.

        IBKR is the source of truth for withholding, so the ladder asks it first and
        the configured assumption last:

        1. ``accrual`` — the open accrual's own tax over its gross: the rate IBKR will
           apply to the very next payment.
        2. ``ibkr_measured`` — the median rate on that security's IBKR payments.
        3. ``ibkr_country`` — the median over IBKR payments from securities domiciled
           in the same country, read off the ISIN's first two letters. A payer IBKR
           has not paid yet usually shares a treaty rate with one it has (every US
           payer on this account had exactly 15% withheld).
        4. ``assumed`` — the WHT setting, the only rung that is not a measurement, and
           the one whose projections stay labelled `gross_estimate`.

        The median rather than the mean on rungs 2–3: a withholding posted on a
        different day from its dividend leaves one row at 0%, and one such row must
        not move a rate every other payment agrees on.
        """
        def _rate(gross, wht) -> Optional[Decimal]:
            if gross is None or gross <= 0 or wht is None:
                return None
            r = abs(wht) / gross
            return r if r < 1 else None

        per_sec: Dict[int, List[Decimal]] = defaultdict(list)
        per_country: Dict[str, List[Decimal]] = defaultdict(list)

        def _country(sid: int) -> Optional[str]:
            isin = getattr(securities.get(sid), "isin", None)
            return isin[:2].upper() if isin and len(isin) >= 2 else None

        for p in raw_payments:
            if p.source != "ibkr":
                continue
            r = _rate(p.gross_amount_eur, p.withholding_tax_eur)
            if r is None:
                continue
            per_sec[p.security_id].append(r)
            country = _country(p.security_id)
            if country:
                per_country[country].append(r)

        assumed = Decimal(1) - net_factor
        out: Dict[int, Tuple[Decimal, str]] = {}
        for sid in securities:
            open_rates = [
                r for a in sorted(accruals_by_sec.get(sid, ()),
                                  key=lambda a: a["ex_date"] or a["pay_date"], reverse=True)
                if (r := _rate(a.get("gross_eur"), a.get("wht_eur"))) is not None
            ]
            country = _country(sid)
            if open_rates:
                out[sid] = (open_rates[0], "accrual")
            elif per_sec.get(sid):
                out[sid] = (median(per_sec[sid]), "ibkr_measured")
            elif country and per_country.get(country):
                out[sid] = (median(per_country[country]), "ibkr_country")
            else:
                out[sid] = (assumed, "assumed")
        return out

    async def _forecast_inputs(
        self,
        raw_payments: List,
        securities: Dict[int, Security],
        as_of: date,
        net_factor: Decimal,
        accruals_by_sec: Optional[Dict[int, List[Dict]]] = None,
        date_pairs: Optional[Dict[int, List[Tuple[date, date]]]] = None,
    ) -> tuple:
        """
        Everything ``project_dividends`` needs, assembled once.

        Split out because the same inference now has to be driven over two
        horizons — the year the caller selected (the chart) and a rolling one
        (the growth figures and the calendar, which must not change when the year
        filter does). Assembling twice would be waste; assembling in two places
        would be a second copy of these rules free to drift from this one, which
        is the failure mode this codebase keeps hitting.

        Returns ``(hist_by_sec, basis_by_sec, lots_by_sec, shares_at, ex_dated,
        wht_by_sec)``, where ``ex_dated`` is the set of securities whose cadence came
        from the yfinance ex-date series. The caller needs it to know what a projected
        date MEANS: for those securities it is an ex-date and has to be shifted to the
        expected pay date, and for the rest it is already a pay date and must not be
        shifted twice. ``wht_by_sec`` is `_withholding_rates`.

        **IBKR is the source of truth for the amounts** (since 2026-10-04). The DATES
        still come from one series — Yahoo's ex-dates wherever it has two or more, see
        *Infer cadence from ONE dated series* in docs/dividends.md — but the amount
        each of those dividends is sized at is IBKR's wherever IBKR paid it, its
        withholding is IBKR's ladder above, and an open accrual is the newest amount of
        all. Until then the IBKR rows of every Yahoo-scheduled security were dropped
        before the line that would have read them: all 22 payers on production were
        sized from Yahoo's gross, and NVDA, which IBKR had paid at 0.25 a quarter,
        projected about 0.01.
        """
        accruals_by_sec = accruals_by_sec or {}
        date_pairs = date_pairs or {}
        taxlots = list((await self.db.execute(select(TaxLot))).scalars().all())
        lots_by_sec: Dict[int, List[TaxLot]] = defaultdict(list)
        for tl in taxlots:
            lots_by_sec[tl.security_id].append(tl)

        def shares_at(sid: int, d: date) -> Decimal:
            total = Decimal("0")
            for lot in lots_by_sec.get(sid, []):
                if lot.open_date > d:
                    continue
                if lot.close_date and lot.close_date <= d:
                    continue
                total += lot.quantity
            return total

        wht_by_sec = self._withholding_rates(
            raw_payments, securities, accruals_by_sec, net_factor
        )

        # Built from the RAW history, not the era-spliced income: a payout from
        # before we owned the share still evidences the schedule, and its
        # per-share amount still sizes the next one. Keying on realized income
        # left every recently-bought payer — TSMC, Samsung, SK Hynix, HPE, the
        # SOXQ ETF — forecasting nothing.
        #
        # Every amount is converted at ONE rate per currency, the newest cached: a
        # payment that has not happened is best sized at today's rate, and one rate
        # across the whole history means exchange-rate moves cannot make a level payer
        # look like a varying one (`dividend_forecast.is_steady`).
        ibkr_rows = [p for p in raw_payments if p.source == "ibkr" and self._is_income(p)]
        fx_to_eur = await self._latest_fx_to_eur(
            {s.currency for s in securities.values() if s.currency}
            | {p.currency for p in ibkr_rows if p.currency},
            as_of,
        )

        # Cadence must come from ONE dated series. The same dividend is recorded
        # twice — yfinance under its ex-date, IBKR under its pay date — and the
        # two sit weeks apart, which halves the apparent gap: ASML's quarterly
        # schedule read as 74 days, 5 payouts a year instead of 4. Deduplication
        # cannot separate them, because Mastercard's ex-to-pay lag of 29 days is
        # longer than a monthly payer's whole cycle. yfinance carries the complete,
        # regular ex-date series, so where it exists it alone defines the schedule.
        #
        # But note the cost of that rule: the chosen series is trusted absolutely.
        # When SBI's two estimate rows turned out to have come from the wrong
        # ticker, they alone projected a monthly schedule for a company that does
        # not pay one, and the real payment was skipped. Hence forecast_samples on
        # the response and the provenance check in SchedulerService — the rule
        # stays, but a thin or suspect inference now says so.
        per_share_rows = defaultdict(list)
        for p in raw_payments:
            if p.amount_per_share is not None:
                per_share_rows[p.security_id].append(p)
        schedule_source = {
            sid: rows for sid, rows in per_share_rows.items() if len(rows) >= 2
        }

        # Which IBKR payment records which Yahoo dividend: the same pairing the era
        # splice and the measured lag use, so the three cannot disagree about it.
        paired = match_estimates_to_ibkr(
            [p for sid, rows in schedule_source.items() for p in rows],
            [p for p in ibkr_rows if p.security_id in schedule_source],
        )
        ibkr_for_estimate = {id(est): row for est, row in paired}
        paired_ibkr = {id(row) for _, row in paired}
        ex_for_pay = {
            (sid, pay): ex for sid, pairs in date_pairs.items() for ex, pay in pairs
        }

        async def _ibkr_per_share(p, ex: Optional[date]) -> Optional[Decimal]:
            """IBKR's gross per share in the payment's currency: the rate its cash line
            states, else what it paid over the shares held on the ex-date."""
            if p.per_share_native is not None and p.per_share_native > 0:
                return p.per_share_native
            pay = p.pay_date or p.ex_date
            shares = shares_at(p.security_id, ex or pay)
            if shares <= 0 or not p.gross_amount_eur or not p.currency:
                return None
            if p.currency == "EUR":
                return p.gross_amount_eur / shares
            # Cache-only, like everything this endpoint reads: the pay date's own
            # rate turns the stored EUR back into the currency it was paid in.
            recent = await self.currency_service._get_most_recent_rate(
                p.currency, pay, "EUR"
            )
            if not recent or not recent[0]:
                return None
            return p.gross_amount_eur / shares / recent[0]

        def _to_eur_ps(native: Optional[Decimal], currency: Optional[str]) -> Optional[Decimal]:
            if native is None or native <= 0:
                return None
            rate = fx_to_eur.get(currency or "EUR")
            return native * rate if rate is not None else None

        hist_by_sec: Dict[int, List[HistPayment]] = defaultdict(list)
        for p in raw_payments:
            sec = securities.get(p.security_id)
            if sec is None:
                continue
            scheduled = p.security_id in schedule_source
            if scheduled and p.amount_per_share is None:
                continue    # IBKR's record of a Yahoo dividend: sized through its pair
            if p.source == "ibkr" and not self._is_income(p):
                continue
            on_date = p.pay_date or p.ex_date
            if on_date is None or on_date > as_of:
                continue

            special = False
            if p.source == "ibkr":
                # An IBKR-only security: its own pay-dated rows are the schedule.
                gross_ps = _to_eur_ps(await _ibkr_per_share(p, None), p.currency)
                special = p.dividend_kind in SPECIAL_DIVIDEND_KINDS
            else:
                gross_ps = _to_eur_ps(p.amount_per_share, sec.currency)
                if gross_ps is None and self._is_income(p) \
                        and p.shares_held and p.shares_held > 0:
                    # No cached rate for the currency today. A row computed while
                    # shares were held already carries the EUR amount at its own
                    # ex-date rate; over the shares, that is the gross per share.
                    gross_ps = self._net_eur(p) / p.shares_held
                ibkr = ibkr_for_estimate.get(id(p))
                if ibkr is not None:
                    special = ibkr.dividend_kind in SPECIAL_DIVIDEND_KINDS
                    ibkr_ps = _to_eur_ps(await _ibkr_per_share(ibkr, p.ex_date), ibkr.currency)
                    # IBKR's figure wins — unless it disagrees with Yahoo's by more
                    # than half. Yahoo restates history for a split and IBKR's cash
                    # line keeps the rate it paid, so a pre-split IBKR rate against
                    # today's post-split share count would size the forecast several
                    # times too high. The money is identical either way; only the
                    # per-share unit differs.
                    if ibkr_ps is not None and (
                        gross_ps is None
                        or Decimal("2") / 3 <= ibkr_ps / gross_ps <= Decimal("1.5")
                    ):
                        gross_ps = ibkr_ps
            hist_by_sec[p.security_id].append(
                HistPayment(on_date=on_date, per_share_eur=gross_ps, special=special)
            )

        # An IBKR payment no Yahoo row records — Yahoo missed it, or the history is
        # older than Yahoo's reach. Its amount is evidence; its pay date is not part of
        # the ex-date series, so it sizes without bending the schedule.
        for p in ibkr_rows:
            if p.security_id not in schedule_source or id(p) in paired_ibkr:
                continue
            pay = p.pay_date or p.ex_date
            if pay is None or pay > as_of:
                continue
            ex = ex_for_pay.get((p.security_id, pay))
            hist_by_sec[p.security_id].append(HistPayment(
                on_date=ex or pay,
                per_share_eur=_to_eur_ps(await _ibkr_per_share(p, ex), p.currency),
                special=p.dividend_kind in SPECIAL_DIVIDEND_KINDS,
                cadence=False,
            ))

        # An open accrual is the newest amount there is: IBKR has declared it. It
        # replaces the figure of the dividend it records when that is already in the
        # history (Yahoo writes the ex-date a day after it passes), and otherwise joins
        # it as amount-only evidence, so a declared raise carries into every later
        # projection the day IBKR lists it — TSMC's 7.00 a share instead of the 4.78
        # the old median kept repeating after it.
        for sid, accruals in accruals_by_sec.items():
            if sid not in securities:
                continue
            for a in accruals:
                qty, gross = a.get("quantity"), a.get("gross_eur")
                ex = a.get("ex_date")
                if ex is None or not qty or qty <= 0 or not gross or gross <= 0:
                    continue
                ps = gross / qty     # already EUR at the newest cached rate
                entries = hist_by_sec[sid]
                # Only an ex-dated series can hold the same dividend under this date;
                # an IBKR-only security's entries are pay dates, weeks later.
                same = next((i for i, e in enumerate(entries)
                             if abs((e.on_date - ex).days) <= ACCRUAL_MATCH_DAYS), None) \
                    if sid in schedule_source else None
                if same is not None:
                    entries[same] = HistPayment(
                        on_date=entries[same].on_date, per_share_eur=ps,
                        special=entries[same].special, cadence=entries[same].cadence,
                    )
                else:
                    entries.append(HistPayment(on_date=ex, per_share_eur=ps, cadence=False))

        # Gross to expected net, one rate per security from the ladder. Applied once
        # here, so every projection — forward, overdue, two years out — carries the
        # same withholding as the payment it repeats.
        basis_by_sec: Dict[int, str] = {}
        for sid, entries in hist_by_sec.items():
            rate, source = wht_by_sec.get(sid, (Decimal(1) - net_factor, "assumed"))
            hist_by_sec[sid] = [
                HistPayment(
                    on_date=e.on_date,
                    per_share_eur=_estimated_net_from_gross(e.per_share_eur, Decimal(1) - rate),
                    special=e.special, cadence=e.cadence,
                )
                for e in entries
            ]
            basis_by_sec[sid] = "gross_estimate" if source == "assumed" else "net"

        return (hist_by_sec, basis_by_sec, lots_by_sec, shares_at, set(schedule_source),
                wht_by_sec)

    async def get_dividend_summary(self) -> Dict:
        """
        Aggregate computed dividends into a monthly summary (in base currency).

        The history is era-spliced: estimates before the first IBKR payment, real
        IBKR rows from there on (see _splice_by_era — the old global switch dropped
        every pre-IBKR month from the card). Monthly amounts and totals are NET
        of withholding tax; gross and withholding totals are reported separately.
        """
        payments, ibkr_from = self._splice_by_era(await self.repo.get_computed_dividends())
        payments = [p for p in payments if self._is_income(p)]

        # Project EUR amounts into the configured base currency at each date.
        from app.services.portfolio_service import PortfolioService
        base_fx = await PortfolioService(self.db)._load_base_fx()

        monthly: Dict[str, Decimal] = defaultdict(Decimal)
        total_net = Decimal("0")
        total_gross = Decimal("0")
        total_wht = Decimal("0")

        now = utcnow()
        ytd_net = Decimal("0")

        for p in payments:
            on_date = p.pay_date or p.ex_date
            gross_e = p.gross_amount_eur or Decimal("0")
            net_e = p.net_amount_eur if p.net_amount_eur is not None else gross_e
            wht_e = p.withholding_tax_eur or Decimal("0")

            gross = base_fx.convert(gross_e, on_date)
            net = base_fx.convert(net_e, on_date)
            wht = base_fx.convert(wht_e, on_date)

            month_key = on_date.strftime("%Y-%m")
            monthly[month_key] += net
            total_net += net
            total_gross += gross
            total_wht += wht
            if on_date.year == now.year:
                ytd_net += net

        monthly_list = [
            {"month": k, "amount_eur": round(float(v), 2)}
            for k, v in sorted(monthly.items())
        ]

        last_updated = None
        if payments:
            latest = max((p.last_computed for p in payments if p.last_computed), default=None)
            if latest:
                # Naive UTC in the column; tag it, or the browser parses it as local
                # (the same misread utc_iso() exists to prevent on sync_runs).
                last_updated = utc_iso(latest)

        return {
            "monthly": monthly_list,           # NET per month
            "ytd_eur": round(float(ytd_net), 2),
            "total_eur": round(float(total_net), 2),        # NET (back-compat key)
            "total_gross_eur": round(float(total_gross), 2),
            "total_withholding_eur": round(float(total_wht), 2),
            "total_net_eur": round(float(total_net), 2),
            # Three-way, like the tax report's flag: once the ledger starts the card
            # still carries the estimated months that precede it, so a flat "ibkr"
            # would claim real withholding for a period that has none.
            "source": _summary_source(payments, ibkr_from),
            "ibkr_from": ibkr_from.isoformat() if ibkr_from else None,
            "last_updated": last_updated,
            "base_currency": base_fx.base_currency,
        }

    async def get_dividend_breakdown(
        self,
        year: Optional[int] = None,
        include_forecast: bool = True,
        as_of: Optional[date] = None,
        period: Optional[Literal["24m"]] = None,
    ) -> Dict:
        """
        Dividends grouped by month × symbol plus per-security totals, optionally
        with forecast projections for the months after ``as_of``.

        Reads only cached data — dividend_payments, taxlots, market_prices and
        exchange_rates — never Yahoo or IBKR. The history is era-spliced like the
        summary; forecasts are inferred per held security from its own payout
        cadence and per-share amounts (see dividend_forecast.py). ``year`` may be
        a future one, in which case the whole year is forecast. ``as_of`` is
        injectable purely so tests can pin the forecast horizon.
        """
        as_of = as_of or date.today()
        net_factor = await AppSettingsRepository(self.db).get_dividend_net_factor()
        if period is not None and (period != "24m" or year is not None):
            raise ValueError("Choose either a year or period='24m'")
        current_month = as_of.strftime("%Y-%m")
        # The forecast reads the RAW history: a zero row means "held nothing at that
        # ex-date", which says nothing about whether the company pays — and its date
        # is evidence of the schedule. Only the realized figures are filtered.
        raw_payments = await self.repo.get_computed_dividends()
        all_payments, ibkr_from = self._splice_by_era(raw_payments)
        all_payments = [p for p in all_payments if self._is_income(p)]

        securities = {
            s.id: s for s in (await self.db.execute(select(Security))).scalars().all()
        }

        from app.services.portfolio_service import PortfolioService
        portfolio = PortfolioService(self.db)
        base_fx = await portfolio._load_base_fx()

        # Future years are selectable: a full year of projections is the point of
        # having a cadence at all, and the next year is the one being planned.
        years = sorted(
            {(p.pay_date or p.ex_date).year for p in all_payments}
            | {as_of.year, as_of.year + 1}
        )

        win_start = date(year, 1, 1) if year is not None else None
        win_end = date(year, 12, 31) if year is not None else None
        if period == "24m":
            win_start = date.fromisoformat(self._shift_month(current_month, 23) + "-01")
            win_end = date.fromisoformat(self._shift_month(current_month, -1) + "-01") - timedelta(days=1)

        def _in_window(d: date) -> bool:
            return (win_start is None or d >= win_start) and (win_end is None or d <= win_end)

        def _new_row() -> Dict:
            return {
                "payouts": 0, "gross": Decimal("0"), "wht": Decimal("0"),
                "net": Decimal("0"), "forecast_payouts": 0,
                "forecast_net": Decimal("0"), "sources": set(),
                "forecast_basis": None,
                "forecast_samples": None, "forecast_cadence_days": None,
                "forecast_lag_days": None, "forecast_lag_samples": None,
                "forecast_method": None,
            }

        # A ticker is only a safe chart key while it means one instrument. The same
        # symbol under two ISINs may be one company on two venues (ASML's Amsterdam
        # ordinary and its US listing) or two unrelated companies — SBI is Sprott in
        # Toronto and SBI Holdings in Tokyo — and nothing here can tell those apart,
        # so it takes the venue suffix rather than silently summing them. Two rows
        # sharing an ISIN are the same instrument and still merge.
        isins_per_symbol: Dict[str, set] = defaultdict(set)
        for sec in securities.values():
            isins_per_symbol[sec.symbol].add(sec.isin)
        ambiguous = {sym for sym, isins in isins_per_symbol.items() if len(isins) > 1}

        def _symbol(security_id: int) -> str:
            sec = securities.get(security_id)
            if not sec:
                return f"#{security_id}"
            if sec.symbol in ambiguous and sec.exchange:
                return f"{sec.symbol} ({sec.exchange})"
            return sec.symbol

        monthly_actual: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        monthly_forecast: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        by_sec: Dict[int, Dict] = {}
        total_net = Decimal("0")
        total_forecast = Decimal("0")

        # The UNWINDOWED halves of the same walk. `growth` is defined as derived
        # from the whole payment history — with year=2026 selected the response
        # carries no 2025 months, so no client could derive year-over-year at all —
        # and the rolling twelve-month series needs the same reach for the same
        # reason: a window ending in January is eleven-twelfths outside the year
        # showing it. Keeping the era splice and the per-date FX projection in one
        # place beats growing a second implementation to drift.
        annual_actual: Dict[int, Decimal] = defaultdict(Decimal)
        # Withholding per calendar year, for the small "WHT" figure beside each year —
        # a reminder of the DA-1 threshold, not a tax figure. IBKR's rows carry what was
        # withheld; an estimate-era row recorded none, so its gross is kept here and
        # sized with the security's ladder rate once that is known (below).
        annual_wht_actual: Dict[int, Decimal] = defaultdict(Decimal)
        annual_wht_forecast: Dict[int, Decimal] = defaultdict(Decimal)
        estimate_gross_by_year: Dict[Tuple[int, int], Decimal] = defaultdict(Decimal)
        month_actual_all: Dict[str, Decimal] = defaultdict(Decimal)
        month_sources_all: Dict[str, set] = defaultdict(set)
        month_actual_sym_all: Dict[str, Dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )

        for p in all_payments:
            on_date = p.pay_date or p.ex_date
            mk = on_date.strftime("%Y-%m")
            net = base_fx.convert(self._net_eur(p), on_date)
            symbol = _symbol(p.security_id)
            # These four accumulate BEFORE the window guard, and that order is the
            # whole point: hoist the `continue` above them and growth quietly
            # starts agreeing with the selected year instead of the history.
            # `test_calendar_ttm_uses_history_outside_each_display_window` and
            # `test_growth_is_identical_whichever_year_is_selected` pin it.
            annual_actual[on_date.year] += net
            if p.source == "ibkr":
                annual_wht_actual[on_date.year] += base_fx.convert(
                    abs(p.withholding_tax_eur or Decimal("0")), on_date
                )
            else:
                estimate_gross_by_year[(on_date.year, p.security_id)] += net
            month_actual_all[mk] += net
            month_actual_sym_all[mk][symbol] += net
            month_sources_all[mk].add(p.source)
            if not _in_window(on_date):
                continue
            row = by_sec.setdefault(p.security_id, _new_row())
            row["payouts"] += 1
            row["net"] += net
            row["gross"] += base_fx.convert(p.gross_amount_eur or Decimal("0"), on_date)
            row["wht"] += base_fx.convert(p.withholding_tax_eur or Decimal("0"), on_date)
            row["sources"].add("ibkr" if p.source == "ibkr" else "estimate")
            monthly_actual[mk][symbol] += net
            total_net += net

        # Projections, in base currency, needed at three different reaches:
        #   - inside the selected window  -> the chart and the per-security table
        #   - the next 365 days           -> the "next 12M" figure and the calendar
        #   - per calendar year           -> the year-over-year comparison
        # One projection pass serves all three. project_dividends() steps
        # deterministically from the last known payment, so a wide projection
        # sliced to a narrower window is identical to projecting that window
        # directly — which is what keeps the chart's numbers unchanged here.
        upcoming: List[Dict] = []
        annual_forecast: Dict[int, Decimal] = defaultdict(Decimal)
        # Projected income per symbol per month, UNWINDOWED and reaching the full
        # horizon. The rolling TTM series needs months the chart never draws, the
        # same way `growth` needs payments the chart never draws. Deliberately NOT
        # `monthly_forecast`, which is capped twice — to the selected window and to
        # chart_end — so every twelve-month window straddling either cap would be
        # silently short.
        month_forecast_sym_all: Dict[str, Dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        # Whether the cadence projected anything AHEAD of today, which is what
        # decides how far the rolling series runs. Not `bool(month_forecast_sym_all)`
        # any more: that dict now also carries calendar entries whose date has gone
        # by, and those justify no reach at all. See `_rolling_twelve_months`.
        has_forward_projection = False
        next_12m = Decimal("0")
        # The same 365-day total, split per security, for the forward yields. Kept
        # beside the aggregate rather than derived from the per-security table
        # afterwards, because a table row is limited to the SELECTED window while a
        # yield must always describe the next twelve months — with ?year=2026 asked
        # in August, `forecast_net` covers Aug-Dec and would read 5/12 of the truth.
        next_12m_by_sec: Dict[int, Decimal] = defaultdict(Decimal)
        next_pay: Dict[int, date] = {}
        # Filled by `_forecast_inputs`; empty without a forecast, which every reader of
        # it below tolerates.
        wht_by_sec: Dict[int, Tuple[Decimal, str]] = {}
        next_12m_end = as_of + timedelta(days=365)
        # Far enough to complete next calendar year, so the year comparison never
        # shows a truncated bar; further still if the caller asked for a year
        # beyond that. Defined out here because the rolling series ends at the
        # horizon too, and re-deriving a two-line expression is how one reach
        # quietly becomes two.
        horizon_end = max(win_end or date.min, date(as_of.year + 1, 12, 31))

        if include_forecast:
            # The announced dividends IBKR has published and not yet paid, and IBKR's own
            # (ex-date, pay date) pairs. Read first: both size the projections as well as
            # dating them.
            accruals_by_sec = await self._open_accruals()
            exact_pairs = await self._exact_date_pairs()
            hist_by_sec, basis_by_sec, lots_by_sec, shares_at, ex_dated, wht_by_sec = \
                await self._forecast_inputs(
                    raw_payments, securities, as_of, net_factor,
                    accruals_by_sec=accruals_by_sec, date_pairs=exact_pairs,
                )

            # What the ex→pay distance actually measures out at, per security.
            pay_lags = self._measured_pay_lags(raw_payments, exact_pairs)

            def _expected_wht(net: Decimal, sid: int) -> Decimal:
                """The withholding behind a projected net amount: net x r / (1 - r) at
                the security's ladder rate — the same rate that took it from gross."""
                rate, _ = wht_by_sec.get(sid, (Decimal(1) - net_factor, "assumed"))
                return net * rate / (Decimal(1) - rate) if rate < 1 else Decimal("0")

            def _accrual_covers(sid: int, ex: date, pay: Optional[date] = None) -> bool:
                """
                True when an open accrual records the dividend that went (or goes) ex on
                ``ex`` — IBKR's announcement outranks every other source for both dates.

                Matched on the EX-date, because that is the date every other source
                actually knows: an inferred pay date is a guess, and with no measured lag
                it IS the ex-date. IBKR pays up to a month later (22 and 29 days on held
                payers), so the old pay-date match let the guess sit beside IBKR's own row
                — one dividend shown and forecast twice. An accrual without an ex-date
                falls back to its pay date against ``pay``.

                One rule for the forward loop and the estimate tail, so the two cannot
                disagree about which dividend an accrual is.
                """
                for a in accruals_by_sec.get(sid, ()):
                    if a["ex_date"] is not None:
                        if abs((a["ex_date"] - ex).days) <= ACCRUAL_MATCH_DAYS:
                            return True
                    elif pay is not None and \
                            abs((a["pay_date"] - pay).days) <= ACCRUAL_MATCH_DAYS:
                        return True
                return False

            horizon_start = as_of + timedelta(days=1)

            # The CHART's reach is unchanged by that widening: without a selected
            # year it still stops at the end of the current year. Projecting
            # further for the growth figures must not quietly stretch the all-time
            # chart into next year — that alone tripled its forecast total.
            chart_end = win_end or date(as_of.year, 12, 31)

            # Projections whose date has passed, collected by the loop and emitted
            # further down beside the accrual and estimate tails.
            overdue: List[Tuple[int, ForecastPayment, int, str, Optional[str]]] = []

            for sid in lots_by_sec:
                qty = shares_at(sid, as_of)
                if qty <= 0:
                    continue
                lag_days, lag_samples = pay_lags.get(sid, (0, 0))
                # Only a cadence inferred from the yfinance ex-date series is an
                # ex-date series. A security whose schedule came from IBKR rows is
                # already pay-dated, and shifting it would push it a lag into the
                # future twice over.
                lag = lag_days if (sid in ex_dated and lag_samples) else 0
                # Two separate reasons to start before the horizon, and the earlier
                # of them wins. A lag back, so a payment whose ex-date has just passed
                # but whose cash is still ahead is produced rather than lost; and
                # PENDING_MAX_AGE_DAYS back, so one whose expected date has ALREADY
                # gone by is produced too. Written as a `min` rather than by assuming
                # which is earlier: the pending tail's bound must not silently depend
                # on PENDING_MAX_AGE_DAYS staying larger than a measured lag.
                #
                # The second widening is split back off below and reaches the calendar
                # only. It is also what bounds that tail — no older ex-date can be
                # generated — so the oldest inferred payment on the calendar is
                # `as_of - PENDING_MAX_AGE_DAYS`, exactly the cutoff the estimate tail
                # applies to a recorded one.
                projected = project_dividends(
                    hist_by_sec.get(sid, []), qty,
                    min(horizon_start - timedelta(days=lag),
                        as_of - timedelta(days=PENDING_MAX_AGE_DAYS)),
                    horizon_end,
                    as_of=as_of,
                )
                if lag:
                    projected = [
                        ForecastPayment(on_date=fp.on_date + timedelta(days=lag),
                                        net_eur=fp.net_eur, method=fp.method)
                        for fp in projected
                    ]
                    # The horizon was applied to the ex-date; re-apply it to the date
                    # the money actually lands on. A payment going ex inside the
                    # horizon and paying outside it is paid outside it.
                    projected = [fp for fp in projected if fp.on_date <= horizon_end]
                if not projected:
                    continue

                symbol = _symbol(sid)
                basis = basis_by_sec.get(sid)
                # An announced dividend beats an inferred one outright, so an inferred
                # payment IBKR has accrued is the same dividend counted twice. The
                # accrual itself is emitted below. Filtered before the overdue split, so
                # both the forward and the overdue tails inherit it.
                projected = [
                    fp for fp in projected
                    if not _accrual_covers(sid, fp.on_date - timedelta(days=lag), fp.on_date)
                ]
                if not projected:
                    continue
                pay_date_source = "measured_lag" if lag else "ex_date"

                # A projection whose own date has gone by is not a forecast any more:
                # the payment was expected and nothing has arrived. Split it off HERE,
                # so the lines below — next_pay, next_12m, the annual and monthly
                # accumulators — see only what is still ahead. The rest are held back
                # and emitted with the other two pending sources, behind the guards
                # that ask whether something better already records the same dividend;
                # the fold at the end of the block then puts them in the forecast
                # buckets under the one rule that covers all three.
                overdue.extend((sid, fp, lag, pay_date_source, basis)
                               for fp in projected if fp.on_date <= as_of)
                projected = [fp for fp in projected if fp.on_date > as_of]
                if not projected:
                    continue
                next_pay[sid] = min(fp.on_date for fp in projected)

                for fp in projected:
                    amt = base_fx.convert(fp.net_eur, fp.on_date)
                    annual_forecast[fp.on_date.year] += amt
                    annual_wht_forecast[fp.on_date.year] += _expected_wht(amt, sid)
                    month_forecast_sym_all[fp.on_date.strftime("%Y-%m")][symbol] += amt
                    if fp.on_date <= next_12m_end:
                        next_12m += amt
                        next_12m_by_sec[sid] += amt
                        upcoming.append({
                            "date": fp.on_date.isoformat(),
                            "ex_date": (fp.on_date - timedelta(days=lag)).isoformat(),
                            "security_id": sid,
                            "symbol": symbol,
                            "net_eur": round(float(amt), 2),
                            "basis": basis,
                            "pay_date_source": pay_date_source,
                            # How the AMOUNT was chosen, beside how the date was:
                            # `latest_payment` (a steady payer's newest, carrying any
                            # raise) or `same_payment_last_year` (a varying payer).
                            "amount_source": fp.method,
                            "pending": False,
                        })

                # Windowed figures stay exactly as before: a security only earns a
                # table row (and a forecast_basis) when it projects INSIDE the
                # selected window, so asking for a past year still returns no
                # forecast-only rows.
                in_window = [fp for fp in projected
                             if _in_window(fp.on_date) and fp.on_date <= chart_end]
                if in_window:
                    by_sec.setdefault(sid, _new_row())["forecast_basis"] = basis
                    # How thin the inference is. A projection off two samples is a
                    # guess with a schedule attached — SBI's five payouts rested on
                    # exactly two rows from the wrong ticker, and nothing on the page
                    # said so. Recorded per security so the UI can badge it.
                    history = hist_by_sec.get(sid, [])
                    by_sec[sid]["forecast_samples"] = len(history)
                    by_sec[sid]["forecast_cadence_days"] = infer_gap_days(
                        [h.on_date for h in history]
                    )
                    # How far the projected dates were moved, and off how many
                    # observations. Absent when nothing was measured, so the UI can say
                    # "this date is an ex-date" rather than imply a settled schedule —
                    # the same reason `forecast_samples` rides along beside the cadence.
                    by_sec[sid]["forecast_lag_days"] = lag if lag else None
                    by_sec[sid]["forecast_lag_samples"] = lag_samples or None
                    by_sec[sid]["forecast_method"] = in_window[0].method
                for fp in in_window:
                    amt = base_fx.convert(fp.net_eur, fp.on_date)
                    row = by_sec[sid]
                    row["forecast_payouts"] += 1
                    row["forecast_net"] += amt
                    monthly_forecast[fp.on_date.strftime("%Y-%m")][symbol] += amt
                    total_forecast += amt

            has_forward_projection = bool(month_forecast_sym_all)

            # ---- Announced, and gone-ex-but-unpaid -----------------------------
            # Everything above projects payments that have not happened yet. These three
            # blocks carry the ones that HAVE — a dividend is announced, or has already
            # gone ex, and its cash simply has not reached the account.
            #
            # Until now that window was invisible. The projection for the payment
            # disappears on its own date, and `_splice_by_era` drops the estimate
            # recording it the moment the IBKR era has begun, so between the ex-date and
            # the cash arriving the dividend was in no figure at all — measured at up to
            # 29 days on this account, on six held securities at once.
            #
            # Each block appends to `upcoming` and records the same payment in
            # `calendar_folds`, which one pass below folds into the forecast
            # accumulators. Collected rather than folded in place: the three blocks
            # differ only in where the payment came from and what guards it had to
            # clear, and three copies of the fold is how the two of them that get
            # edited together stop agreeing with the third.
            # The last field says whether the entry is our inference from the cadence
            # (the overdue block) rather than a record (an accrual, a Yahoo row): only an
            # inference has a sample count to report.
            calendar_folds: List[Tuple[date, str, int, Decimal, Optional[str], bool]] = []

            for sid, accruals in accruals_by_sec.items():
                if shares_at(sid, as_of) <= 0:
                    continue
                for a in accruals:
                    if a["pay_date"] > next_12m_end:
                        continue
                    amt = base_fx.convert(a["net_eur"], a["pay_date"])
                    upcoming.append({
                        "date": a["pay_date"].isoformat(),
                        "ex_date": a["ex_date"].isoformat() if a["ex_date"] else None,
                        "security_id": sid,
                        "symbol": _symbol(sid),
                        "net_eur": round(float(amt), 2),
                        # IBKR accrues net of the withholding it will deduct, so this
                        # needs no `gross_estimate` caveat the way an inference does.
                        "basis": "net",
                        "pay_date_source": "accrual",
                        "amount_source": "announced",
                        "pending": a["pay_date"] <= as_of,
                    })
                    calendar_folds.append(
                        (a["pay_date"], _symbol(sid), sid, amt, "net", False, "announced")
                    )

            ibkr_pays_by_sec: Dict[int, List[Tuple[date, int]]] = defaultdict(list)
            est_ex_by_sec: Dict[int, List[date]] = defaultdict(list)
            for p in raw_payments:
                if p.source == "ibkr":
                    ibkr_pays_by_sec[p.security_id].append(
                        (p.pay_date or p.ex_date, max_pay_lag_days(p.currency))
                    )
                elif (p.ex_date or p.pay_date) is not None:
                    est_ex_by_sec[p.security_id].append(p.ex_date or p.pay_date)

            def _cash_has_landed(sid: int, ex: date) -> bool:
                """
                True when an IBKR payment sits inside the lag window after ``ex`` —
                the same test `match_estimates_to_ibkr` makes, asked of one date.

                One implementation for both pending tails deliberately. Two copies
                of this window would be free to drift, and the drifted one would go
                on looking right: a dividend still shown as owed after the cash
                arrived, or dropped before it did.
                """
                return any(ex < pay <= ex + timedelta(days=window)
                           for pay, window in ibkr_pays_by_sec.get(sid, ()))

            for p in raw_payments:
                if p.source == "ibkr" or not self._is_income(p):
                    continue
                ex = p.ex_date or p.pay_date
                # Only rows the splice has superseded: before the era began an estimate
                # IS the income and is already counted, so repeating it here would show
                # it twice.
                if ex is None or ibkr_from is None or ex < ibkr_from:
                    continue
                if not (as_of - timedelta(days=PENDING_MAX_AGE_DAYS) <= ex <= as_of):
                    continue
                if shares_at(p.security_id, as_of) <= 0:
                    continue
                if _cash_has_landed(p.security_id, ex):
                    continue
                lag_days, lag_samples = pay_lags.get(p.security_id, (0, 0))
                expected = ex + timedelta(days=lag_days if lag_samples else 0)
                if _accrual_covers(p.security_id, ex, expected):
                    continue  # IBKR has announced it; the accrual above says it better
                # The stored Yahoo "net" is gross. The security's own withholding
                # applies — the same ladder rung `_forecast_inputs` sized it with.
                wht_rate, _ = wht_by_sec.get(
                    p.security_id, (Decimal(1) - net_factor, "assumed")
                )
                est_basis = basis_by_sec.get(p.security_id, "gross_estimate")
                amt = base_fx.convert(
                    _estimated_net_from_gross(self._net_eur(p), Decimal(1) - wht_rate),
                    expected,
                )
                upcoming.append({
                    "date": expected.isoformat(),
                    "ex_date": ex.isoformat(),
                    "security_id": p.security_id,
                    "symbol": _symbol(p.security_id),
                    "net_eur": round(float(amt), 2),
                    "basis": est_basis,
                    "pay_date_source": "measured_lag" if lag_samples else "ex_date",
                    # The real per-share figure on the real share count, not a
                    # projection: Yahoo has recorded this dividend.
                    "amount_source": "estimate",
                    "pending": expected <= as_of,
                })
                calendar_folds.append(
                    (expected, _symbol(p.security_id), p.security_id, amt,
                     est_basis, False, "estimate")
                )

            # The weakest of the three, and the last resort: NOTHING records this
            # payment. The cadence says one was due, the measured lag (or its absence)
            # says by when, and no estimate row, accrual or cash has appeared.
            #
            # That state is not exotic — yfinance writes a dividend into its series the
            # day AFTER the ex-date, so every payer passes through it on every cycle.
            # VT went ex on 2026-09-18 with its next projection dated exactly there, the
            # horizon dropped it that morning, and Yahoo's row was not due until the
            # following evening: for a day and a half the payment was in no figure.
            #
            # Unlike an accrual it EXPIRES. An accrual is IBKR asserting the money is
            # still owed, and stays until IBKR stops saying so; this is us inferring it,
            # and an inference nothing ever confirms has to stop claiming —
            # PENDING_MAX_AGE_DAYS bounds it, through the widened projection start.
            for sid, fp, lag, source, basis in overdue:
                ex = fp.on_date - timedelta(days=lag)
                # Entitlement is fixed by the holding on the ex-date, not today's: a
                # position opened after it is owed nothing, and shares added since would
                # size the payment to an entitlement they never carried.
                held = shares_at(sid, ex)
                if held <= 0:
                    continue
                if _cash_has_landed(sid, ex):
                    continue
                # An estimate row for the same dividend means the block above already
                # shows it, or the splice already counts it as income. In practice the
                # projection cannot even be generated once that row exists, because the
                # cadence steps from the last recorded ex-date — but that is a property
                # of `_forecast_inputs`, and what has to hold here is asserted here.
                if any(abs((d - ex).days) <= ACCRUAL_MATCH_DAYS
                       for d in est_ex_by_sec.get(sid, ())):
                    continue
                amt = base_fx.convert(
                    fp.net_eur * held / shares_at(sid, as_of), fp.on_date
                )
                upcoming.append({
                    "date": fp.on_date.isoformat(),
                    "ex_date": ex.isoformat(),
                    "security_id": sid,
                    "symbol": _symbol(sid),
                    "net_eur": round(float(amt), 2),
                    "basis": basis,
                    "pay_date_source": source,
                    "amount_source": fp.method,
                    "pending": True,
                })
                calendar_folds.append(
                    (fp.on_date, _symbol(sid), sid, amt, basis, True, fp.method)
                )

            # ---- The calendar IS the forecast ----------------------------------
            # One rule for all four producers of `upcoming`: money this portfolio
            # expects and has not received belongs in the forecast buckets, dated
            # where the calendar dates it. The forward loop above already folds its
            # own; these three used to reach the calendar and nothing else, so a
            # payment that had actually gone ex — the most certain money on the
            # list — was the only kind missing from every chart and every total.
            # Measured on production 2026-09-19: 28.88 of 114.55.
            #
            # Two directions this must NOT go.
            #
            # Never the realized side. The cash has not arrived; `monthly_actual`,
            # `annual_actual`, `total_net_eur` and `growth.ttm/ytd` stay measured.
            #
            # Never `next_12m` (and so never `forward_yield`) for a payment already
            # due. That figure is as_of -> as_of+365, and a backlog entry is before
            # it, not inside it — folding one in would inflate a run-rate with a
            # payment whose cycle is already counted. A calendar entry dated AHEAD
            # of today is inside it and does belong: an estimate row arriving makes
            # the cadence step past that payment, so excluding it silently cost the
            # forward figures one payment per security until the cash landed.
            for on_date, symbol, sid, amt, basis, inferred, method in calendar_folds:
                annual_forecast[on_date.year] += amt
                annual_wht_forecast[on_date.year] += _expected_wht(amt, sid)
                month_forecast_sym_all[on_date.strftime("%Y-%m")][symbol] += amt
                if on_date > as_of:
                    next_pay[sid] = min(on_date, next_pay.get(sid, date.max))
                    if on_date <= next_12m_end:
                        next_12m += amt
                        next_12m_by_sec[sid] += amt
                if _in_window(on_date) and on_date <= chart_end:
                    row = by_sec.setdefault(sid, _new_row())
                    row["forecast_payouts"] += 1
                    row["forecast_net"] += amt
                    # Only when the cadence did not already label the row. A
                    # security projecting from received dividends is `net`, and one
                    # unsettled gross-sized entry must not relabel its whole row.
                    if row["forecast_basis"] is None:
                        row["forecast_basis"] = basis
                    if row["forecast_method"] is None:
                        row["forecast_method"] = method
                    # An overdue inference rests on the same history as a forward
                    # projection, so it reports how thin that history is the same way.
                    # Without this a security whose only in-window payment was overdue
                    # served `forecast_payouts > 0` beside `forecast_samples: None`, a
                    # projection with its evidence missing (found 2026-10-01, when the
                    # smoke fixture's quarterly payer first fell due on the day).
                    if inferred and row["forecast_samples"] is None:
                        history = hist_by_sec.get(sid, [])
                        row["forecast_samples"] = len(history)
                        row["forecast_cadence_days"] = infer_gap_days(
                            [h.on_date for h in history]
                        )
                    monthly_forecast[on_date.strftime("%Y-%m")][symbol] += amt
                    total_forecast += amt

        upcoming.sort(key=lambda u: (u["date"], u["symbol"]))

        # ---- Growth -------------------------------------------------------------
        # Built on the unwindowed accumulators above, for the reason stated there.
        # `_net_between` stays a separate walk rather than joining them: it answers
        # a DAY-granular question (Jan 1 to the same calendar day last year) that
        # month buckets cannot express.
        def _net_between(after: date, through: date) -> Decimal:
            """Realized net in (after, through], each payment at its own date's rate."""
            total = Decimal("0")
            for payment in all_payments:
                d = payment.pay_date or payment.ex_date
                if after < d <= through:
                    total += base_fx.convert(self._net_eur(payment), d)
            return total

        year_ago, two_years_ago = as_of - timedelta(days=365), as_of - timedelta(days=730)
        ttm_total = _net_between(year_ago, as_of)
        prev_ttm_total = _net_between(two_years_ago, year_ago)

        # Jan 1 -> today against Jan 1 -> the same day last year. Comparing a
        # part-year against a whole prior year is the difference between +99% and
        # +297% on this account, and only one of those is growth.
        ytd_total = _net_between(date(as_of.year, 1, 1) - timedelta(days=1), as_of)
        prev_ytd_total = _net_between(
            date(as_of.year - 1, 1, 1) - timedelta(days=1),
            self._same_day_last_year(as_of),
        )

        # The two 12-month windows sit either side of the splice, so one is IBKR
        # actuals and the other yfinance estimates — sized comparably, sourced
        # differently. Flagged rather than silently presented as like-for-like.
        ttm_crosses_era = (
            ibkr_from is not None and two_years_ago < ibkr_from <= as_of
        )

        first_income = min(
            ((p.pay_date or p.ex_date) for p in all_payments), default=None
        )

        # Estimate-era income recorded no withholding; size it at the security's ladder
        # rate. Without a forecast the ladder was never built, so build it here — it is
        # pure DB, and the figure must not change with the Forecast toggle.
        if estimate_gross_by_year:
            rates = wht_by_sec or self._withholding_rates(
                raw_payments, securities, await self._open_accruals(), net_factor
            )
            for (y, sid), gross in estimate_gross_by_year.items():
                rate, _ = rates.get(sid, (Decimal(1) - net_factor, "assumed"))
                annual_wht_actual[y] += gross * rate

        annual_rows: List[Dict] = []
        prev_row: Optional[Dict] = None
        for y in sorted(set(annual_actual) | set(annual_forecast)):
            actual_y = annual_actual.get(y, Decimal("0"))
            forecast_y = annual_forecast.get(y, Decimal("0"))
            total_y = actual_y + forecast_y
            partial = (
                y > as_of.year
                or (y == as_of.year and as_of < date(y, 12, 31))
                # The earliest year of income starts whenever the first dividend
                # landed, not in January: 2024 holds seven months here, which is
                # why 2025/2024 reads +1300% and means almost nothing.
                or (first_income is not None and y == first_income.year
                    and first_income.month > 1)
            )
            row = {
                "year": y,
                "net_eur": round(float(actual_y), 2),
                "forecast_net_eur": round(float(forecast_y), 2),
                "total_eur": round(float(total_y), 2),
                # Withholding on the received part (IBKR's own figure; estimated at the
                # ladder rate for estimate-era income) and on the projected part. The
                # client shows their sum, or only the first with Forecast off.
                "withholding_eur": round(float(annual_wht_actual.get(y, Decimal("0"))), 2),
                "forecast_withholding_eur": round(
                    float(annual_wht_forecast.get(y, Decimal("0"))), 2
                ),
                "yoy_pct": None,
                "yoy_includes_forecast": False,
                "yoy_vs_partial": False,
                "partial": partial,
            }
            # Only against the immediately preceding year. A gap year is not a
            # comparison — it would silently measure across two years of growth.
            if prev_row is not None and prev_row["year"] == y - 1:
                row["yoy_pct"] = self._pct(total_y, prev_row["total"])
                row["yoy_includes_forecast"] = (
                    forecast_y > 0 or prev_row["has_forecast"]
                )
                row["yoy_vs_partial"] = prev_row["partial"]
            annual_rows.append(row)
            prev_row = {"year": y, "total": total_y, "partial": partial,
                        "has_forecast": forecast_y > 0}

        latest_key = max(
            (k for k, v in month_actual_all.items() if v > 0), default=None
        )
        latest_month = None
        if latest_key is not None:
            latest_value = month_actual_all[latest_key]
            latest_month = {
                "month": latest_key,
                "net_eur": round(float(latest_value), 2),
                "mom_pct": self._pct(
                    latest_value, month_actual_all.get(self._shift_month(latest_key, 1))
                ),
                "yoy_pct": self._pct(
                    latest_value, month_actual_all.get(self._shift_month(latest_key, 12))
                ),
            }

        # Two paces under the monthly average, both per month and both over FINISHED
        # months only — the month in progress may simply not have paid yet, and a
        # finished-month figure is the month bars summed, which a reader can check.
        # They lag by up to a month; the headline average above them does not.
        #  * ytd_pace: this year's average month so far against last year's (÷12).
        #  * recent_pace: the last three months against the three before them.
        #    Rolling months rather than calendar quarters, and three because the
        #    core ETFs pay in Mar/Jun/Sep/Dec: ANY three consecutive months hold
        #    exactly one of those spikes, so the window can move every month and
        #    still compare like with like — where month against month only
        #    measures the payout calendar.
        ytd_pace = None
        recent_pace = None
        income_keys = sorted(k for k, v in month_actual_all.items() if v > 0)
        if income_keys:
            last_full = self._shift_month(as_of.strftime("%Y-%m"), 1)

            def _span_total(end_key: str, months: int) -> Decimal:
                return sum(
                    (month_actual_all.get(self._shift_month(end_key, i), Decimal("0"))
                     for i in range(months)),
                    Decimal("0"),
                )

            ytd_months = as_of.month - 1
            if ytd_months > 0:
                prev_year = as_of.year - 1
                ytd_avg = _span_total(last_full, ytd_months) / ytd_months
                prev_avg = _span_total(f"{prev_year}-12", 12) / 12
                ytd_pace = {
                    "net_eur": round(float(ytd_avg), 2),
                    "prev_net_eur": round(float(prev_avg), 2),
                    "pct": self._pct(ytd_avg, prev_avg),
                    "months": ytd_months,
                    "prev_year": prev_year,
                    # Income that starts partway through last year still gets ÷12,
                    # which understates the base and overstates the growth. Flag
                    # it rather than divide by a coverage the data cannot prove.
                    "prev_year_partial": (
                        income_keys[0][:4] == str(prev_year)
                        and income_keys[0] > f"{prev_year}-01"
                    ),
                }

            prev_end = self._shift_month(last_full, 3)
            recent_avg = _span_total(last_full, 3) / 3
            prior_avg = _span_total(prev_end, 3) / 3
            recent_pace = {
                "net_eur": round(float(recent_avg), 2),
                "prev_net_eur": round(float(prior_avg), 2),
                "pct": self._pct(recent_avg, prior_avg),
                "start": self._shift_month(last_full, 2),
                "end": last_full,
                "prev_start": self._shift_month(prev_end, 2),
                "prev_end": prev_end,
            }

        growth = {
            "ttm": {
                "net_eur": round(float(ttm_total), 2),
                "prev_net_eur": round(float(prev_ttm_total), 2),
                "pct": self._pct(ttm_total, prev_ttm_total),
            },
            "ttm_crosses_era": ttm_crosses_era,
            "ytd": {
                "net_eur": round(float(ytd_total), 2),
                "prev_net_eur": round(float(prev_ytd_total), 2),
                "pct": self._pct(ytd_total, prev_ytd_total),
            },
            "avg_month": {
                "net_eur": round(float(ttm_total / 12), 2),
                "prev_net_eur": round(float(prev_ttm_total / 12), 2),
                # Both sides divided by the same 12, so the growth is the TTM one.
                "pct": self._pct(ttm_total, prev_ttm_total),
            },
            "next_12m_eur": round(float(next_12m), 2),
            # None when no projection was run, rather than the -100.0 that a zero
            # numerator over a real TTM base produces. `_pct` is right to guard only a
            # zero *base* — a quarterly payer has empty months constantly — but a zero
            # numerator here means "nobody asked for a forecast", and rendering that as
            # "dividends will fall 100%" is a figure manufactured by a flag. Production
            # really did serve it on ?forecast=false.
            "next_12m_vs_ttm_pct": (
                self._pct(next_12m, ttm_total) if include_forecast else None
            ),
            "annual": annual_rows,
            "latest_month": latest_month,
            "ytd_pace": ytd_pace,
            "recent_pace": recent_pace,
        }

        # Trailing-12-month yield: spliced net over the current market value.
        # Positions are a decoration here — their failure must not 500 the view.
        ttm_start = as_of - timedelta(days=365)
        ttm_net: Dict[int, Decimal] = defaultdict(Decimal)
        for p in all_payments:
            d = p.pay_date or p.ex_date
            if ttm_start <= d <= as_of:
                ttm_net[p.security_id] += base_fx.convert(self._net_eur(p), d)

        # How much of that trailing year the position was actually held for. A
        # holding bought seven weeks ago divides seven weeks of income by a full
        # position value, so its yield reads a fraction of the truth — SBI showed
        # 1.0% off a single payment. The figure is not wrong to compute, but it is
        # wrong to present unqualified, so report the coverage and let the UI badge
        # it, the same way `yoy_vs_partial` handles the first year of income.
        # One aggregate rather than reusing the forecast's lot map, which only
        # exists when include_forecast is set — this figure must be right either way.
        #
        # Measured as the UNION of the lot intervals clipped to the window, not as
        # `as_of - min(open_date)`. That older form asked "how long ago was this security
        # first bought", which answers the question only while the holding is unbroken:
        # sell out entirely and rebuy months later and it still reports a full year, so a
        # yield built from two partial stretches of income was presented unqualified —
        # the flag under-reporting in exactly the case that most needs it.
        #
        # Intervals are [open_date, close_date) because a lot sold on D is not held at D's
        # close, the same convention as `_calculate_daily_value` and the attribution gates.
        lot_spans = (await self.db.execute(
            select(TaxLot.security_id, TaxLot.open_date, TaxLot.close_date)
        )).all()
        spans_by_sec: Dict[int, List] = defaultdict(list)
        for sid, opened, closed in lot_spans:
            if opened is None:
                continue
            span_start = max(opened, ttm_start)
            span_end = min(closed, as_of) if closed else as_of
            if span_end > span_start:
                spans_by_sec[sid].append((span_start, span_end))

        ttm_days_held: Dict[int, int] = {}
        for sid, spans in spans_by_sec.items():
            # Overlapping lots must not double-count a day: three lots open across the
            # same month is one month held, not three.
            total_days = 0
            cur_start = cur_end = None
            for span_start, span_end in sorted(spans):
                if cur_end is None:
                    cur_start, cur_end = span_start, span_end
                elif span_start <= cur_end:
                    cur_end = max(cur_end, span_end)
                else:
                    total_days += (cur_end - cur_start).days
                    cur_start, cur_end = span_start, span_end
            if cur_end is not None:
                total_days += (cur_end - cur_start).days
            ttm_days_held[sid] = total_days
        # This call must stay the LAST database access in the method. It is allowed to
        # fail into "yields omitted" only because nothing queries afterwards: a
        # DBAPI-level error leaves the AsyncSession needing a rollback, so any later
        # execute() would raise PendingRollbackError and turn a graceful degradation
        # into a 500 on the whole endpoint. Hoisting it to build `growth` in one place
        # is exactly that mistake — the forward yield below is assigned in afterwards
        # for this reason, not for style.
        mv_by_sec: Dict[int, Decimal] = {}
        cost_by_sec: Dict[int, Decimal] = {}
        try:
            for pos in await portfolio.get_positions_breakdown():
                mv_by_sec[pos["security_id"]] = Decimal(str(pos["market_value_eur"]))
                cost_by_sec[pos["security_id"]] = Decimal(str(pos["cost_basis_eur"]))
        except Exception as e:
            logger.warning(f"Dividend breakdown: positions unavailable, yields omitted: {e}")

        # ---- Forward yield ------------------------------------------------------
        # What the portfolio as held today will pay over the next twelve months, over
        # what it is worth and over what it cost. A sibling of `growth` rather than a
        # member of it: `growth` is defined as derived from the unwindowed payment
        # HISTORY, and a figure that moves with a market price is neither growth nor
        # history. Keeping it out also means `DividendKpiCards`, which answers "is this
        # growing", is not handed a valuation ratio.
        #
        # The market-value figure IS the market-value-weighted average of the
        # per-security yields, by construction rather than by coincidence:
        # sum(Vi/V * Di/Vi) == sum(Di)/V, with every non-payer entering at zero. So
        # nothing is weighted by hand, and `forward_yield_pct` on each row below is the
        # audit of this one. Note the table shows only securities with payments or an
        # in-window projection, so averaging the VISIBLE rows alone gives a higher
        # number — the coverage counts are what make that legible.
        #
        # An unpriced holding is excluded from BOTH sides. portfolio_service values a
        # position with no cached price at 0.00, so leaving it in adds its projected
        # income to the numerator and nothing to the denominator, reading the yield
        # high — the SBI shape, and the same refusal rebalance.ts makes: no price means
        # no weight, not a zero weight. Its cost basis IS known and is dropped anyway,
        # because the gap between the two figures is only readable as appreciation
        # while both describe the same set of securities.
        forward_yield: Optional[Dict] = None
        priced = {sid: mv for sid, mv in mv_by_sec.items() if mv > 0}
        fwd_mv = sum(priced.values(), Decimal("0"))
        fwd_income = sum(
            (next_12m_by_sec.get(sid, Decimal("0")) for sid in priced), Decimal("0")
        )
        # A zero numerator yields nothing, not 0.00%. Three different states produce it
        # — no projection was run, nothing held has a schedule, or the one security that
        # does is unpriced and was excluded above — and a 0.00% would report all three
        # as "this portfolio pays no dividends". The last is the dangerous one: it is the
        # SBI shape again, where the *interesting* holding is the missing one. Refusing
        # matches dividend_forecast.py's own rule, and "No projected dividends" on the
        # card says more than a confident zero would.
        if include_forecast and fwd_mv > 0 and fwd_income > 0:
            fwd_cost = sum(
                (cost_by_sec.get(sid, Decimal("0")) for sid in priced), Decimal("0")
            )
            # Part of the projection is sized from yfinance gross per-share, which
            # uses assumed withholding to derive estimated net. Reported as a share of
            # the total rather than a bare flag, so the caveat can be quantified — and
            # rendered in the footnote, not only in a tooltip: a caveat reachable only
            # by hovering does not exist on a touch device.
            # Off `basis_by_sec`, not off the table rows: a row only exists when the
            # security projects INSIDE the selected window, so reading the basis from
            # there would report a security's projection as `net` purely because its
            # next payment falls outside the year being viewed.
            gross_est = sum(
                (
                    next_12m_by_sec.get(sid, Decimal("0"))
                    for sid in priced
                    if basis_by_sec.get(sid) == "gross_estimate"
                ),
                Decimal("0"),
            )
            forward_yield = {
                "annual_eur": round(float(fwd_income), 2),
                "pct": round(float(fwd_income / fwd_mv * 100), 2),
                "on_cost_pct": (
                    round(float(fwd_income / fwd_cost * 100), 2) if fwd_cost > 0 else None
                ),
                # An accumulating ETF counts in `priced_holdings` and not in `paying`,
                # which is the right answer for it — so a low yield reads as "most of
                # this book does not distribute" rather than as missing data.
                "paying_holdings": sum(
                    1 for sid in priced if next_12m_by_sec.get(sid, Decimal("0")) > 0
                ),
                "priced_holdings": len(priced),
                "unpriced_holdings": len(mv_by_sec) - len(priced),
                "gross_estimate_eur": round(float(gross_est), 2),
                "basis": _forward_basis(fwd_income, gross_est),
            }

        if year is not None:
            months_axis = [f"{year:04d}-{m:02d}" for m in range(1, 13)]
        elif period == "24m":
            months_axis = [self._shift_month(current_month, back) for back in range(23, -1, -1)]
        else:
            keys = sorted(set(monthly_actual) | set(monthly_forecast))
            months_axis = []
            if keys:
                # A stopped payer still has elapsed zero-income months. Omitting
                # those would freeze its rolling income at its final payout.
                keys.append(current_month)
                keys.sort()
                y, m = (int(x) for x in keys[0].split("-"))
                last_y, last_m = (int(x) for x in keys[-1].split("-"))
                while (y, m) <= (last_y, last_m):
                    months_axis.append(f"{y:04d}-{m:02d}")
                    y, m = (y, m + 1) if m < 12 else (y + 1, 1)

        ttm_series = self._rolling_twelve_months(
            month_actual_sym_all=month_actual_sym_all,
            month_forecast_sym_all=month_forecast_sym_all,
            month_sources_all=month_sources_all,
            first_income=first_income,
            current_month=current_month,
            horizon_month=horizon_end.strftime("%Y-%m"),
            axis_start=months_axis[0] if months_axis else None,
            axis_end=months_axis[-1] if months_axis else None,
            windowed=year is not None or period == "24m",
            include_forecast=include_forecast,
            has_forward_projection=has_forward_projection,
        )
        # The earliest window that exists over the WHOLE history, regardless of the
        # selected range. The client cannot derive it: with ?year=2026 the response
        # carries only 2026 windows and nothing in it says whether an earlier one
        # exists — which is exactly what decides whether the growth figure's base is
        # coverage-limited (the account was still being funded inside it).
        ttm_coverage_start = (
            self._shift_month(first_income.strftime("%Y-%m"), -11)
            if first_income is not None else None
        )

        # The colour order: every symbol this book has ever been paid by or is
        # projected to be paid by, biggest first. Unwindowed for the same reason
        # `ttm_coverage_start` is, and with one further property the client depends
        # on — it is IDENTICAL for `year=`, `period=24m` and all time, so a symbol
        # cannot change colour when the range changes. The client used to rank the
        # symbols itself out of whatever slice it had been sent, which is precisely
        # why it could not: the top eight of 2025 are not the top eight of 2026, and
        # every symbol after the first difference shifted a palette slot.
        #
        # **Forecast buckets count, and that makes this the one input the order does
        # depend on.** Measured on production: VT, QQQM, 2330, SOXQ and GRID have
        # zero realized income and are the chart's biggest series (VT alone is 45.61
        # of 190), so a realized-only ranking would fold the five largest bars into
        # Other and leave every future planning year colourless. The price is that
        # `forecast=false` — reachable on the route, and asked for by nothing in the
        # app — ranks a different set. The client fetches once WITH projections and
        # hides them in the browser precisely so the toggle stays instant, so no
        # rendering ever crosses that boundary. Pinned both ways in
        # `test_dividend_growth.py`; do not "fix" the flag dependency by dropping the
        # projections from the ranking.
        #
        # The symbol is the tie-break so equal totals cannot flip between requests.
        stack_totals: Dict[str, Decimal] = defaultdict(Decimal)
        for by_symbol in month_actual_sym_all.values():
            for sym, v in by_symbol.items():
                stack_totals[sym] += v
        for by_symbol in month_forecast_sym_all.values():
            for sym, v in by_symbol.items():
                stack_totals[sym] += v
        stack_order = [
            sym for sym, _ in sorted(stack_totals.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        months = []
        for mk in months_axis:
            actual = monthly_actual.get(mk, {})
            forecast = monthly_forecast.get(mk, {})
            # Growth is measured on realized income only. A forecast month's
            # "change" would be an artifact of the projection's own flat median —
            # it would read as the payout schedule shifting when nothing has.
            realized = month_actual_all.get(mk, Decimal("0"))
            months.append({
                "month": mk,
                "actual": {s: round(float(v), 2) for s, v in sorted(actual.items())},
                "forecast": {s: round(float(v), 2) for s, v in sorted(forecast.items())},
                "actual_total_eur": round(float(sum(actual.values(), Decimal("0"))), 2),
                "forecast_total_eur": round(float(sum(forecast.values(), Decimal("0"))), 2),
                "mom_pct": (
                    self._pct(realized, month_actual_all.get(self._shift_month(mk, 1)))
                    if realized > 0 else None
                ),
                "yoy_pct": (
                    self._pct(realized, month_actual_all.get(self._shift_month(mk, 12)))
                    if realized > 0 else None
                ),
            })

        window_total = total_net + total_forecast
        sec_rows = []
        for sid, row in by_sec.items():
            sec = securities.get(sid)
            mv = mv_by_sec.get(sid)
            cost = cost_by_sec.get(sid)
            ttm = ttm_net.get(sid, Decimal("0"))
            sec_rows.append({
                "security_id": sid,
                "symbol": _symbol(sid),
                # Identity is isin + exchange, so the same ticker can appear twice
                # (ASML on NASDAQ and on AEB). The chart merges them — one company,
                # one stack colour — but the table lists them separately, and
                # without the venue the two rows are indistinguishable.
                "exchange": (sec.exchange if sec else None),
                "description": (sec.description or sec.symbol) if sec else f"#{sid}",
                "payouts": row["payouts"],
                "gross_eur": round(float(row["gross"]), 2),
                "withholding_eur": round(float(row["wht"]), 2),
                "net_eur": round(float(row["net"]), 2),
                "forecast_payouts": row["forecast_payouts"],
                "forecast_net_eur": round(float(row["forecast_net"]), 2),
                "trailing_yield_pct": (
                    round(float(ttm / mv * 100), 2)
                    if mv and mv > 0 and ttm > 0 else None
                ),
                # The forward counterpart, and the audit of the portfolio-level figure:
                # weight each of these by its share of market value and the result is
                # `forward_yield.pct` exactly.
                #
                # Deliberately the security's NEXT-12-MONTH projection, not the
                # window-limited `forecast_net_eur` beside it — asked in August with
                # ?year=2026 that field covers Aug-Dec and the yield would read 5/12 of
                # the truth. So this number does not change with the selected year even
                # though which rows appear does, and a row can legitimately show a
                # forecast of 0 next to a non-zero yield when its next payment falls
                # past the window. Don't "fix" them into agreement.
                "forward_yield_pct": (
                    round(float(next_12m_by_sec.get(sid, Decimal("0")) / mv * 100), 2)
                    if mv and mv > 0 and next_12m_by_sec.get(sid, Decimal("0")) > 0
                    else None
                ),
                # True when the position wasn't held for the whole trailing year, so
                # a partial year's income is being divided by a full position value
                # and the yield reads low. Badged, not silently annualized: scaling
                # up would invent income the schedule may not support.
                "trailing_yield_partial": (
                    ttm_days_held.get(sid, 0) < TTM_FULL_COVERAGE_DAYS
                ),
                "days_held_in_ttm": ttm_days_held.get(sid),
                # The FORWARD annual rate over what the position cost — which is what
                # "yield on cost" means, and what the portfolio-level card divides.
                #
                # It used to be trailing income over current cost, and those two
                # describe different positions the moment the position size changes
                # inside the window. Adding to a holding divides a small position's
                # income by the finished position's cost, so the figure reads far too
                # low: on this account MCO showed 0.35% against a real forward 0.84%,
                # SPGI 0.53% against 0.93% — nine of fifteen rows understated, none of
                # them badged, because the `†` partial marker was only ever on the
                # trailing *yield* column. Selling and rebuying is the same defect at
                # its most extreme: the income was earned on shares bought cheaply and
                # is then divided by the cost of the shares that replaced them.
                #
                # Forward over current cost has no such mismatch — the projection is
                # sized on the shares held now, and `cost` is what those shares cost.
                # It also makes this column agree with the Performance tab's *Yield on
                # Cost* card, which computed forward-over-cost from the day it shipped;
                # two figures with one name and two definitions is the failure this
                # codebase names first.
                "yield_on_cost_pct": (
                    round(float(next_12m_by_sec.get(sid, Decimal("0")) / cost * 100), 2)
                    if cost and cost > 0 and next_12m_by_sec.get(sid, Decimal("0")) > 0
                    else None
                ),
                "share_pct": (
                    round(float((row["net"] + row["forecast_net"]) / window_total * 100), 1)
                    if window_total > 0 else None
                ),
                "next_pay_date": (
                    next_pay[sid].isoformat() if sid in next_pay else None
                ),
                # Provenance of the ACTUAL rows; None for a forecast-only entry.
                "source": (
                    ("mixed" if len(row["sources"]) > 1 else next(iter(row["sources"])))
                    if row["sources"] else None
                ),
                # 'net' when the projection is sized from dividends actually
                # received, 'gross_estimate' when only yfinance's gross per-share
                # exists — converted to estimated net with the shared factor.
                "forecast_basis": row["forecast_basis"],
                # How thin the projection's inference is: how many dated payments
                # defined the schedule, and the median gap it settled on. Two
                # samples is a guess with a schedule attached.
                "forecast_samples": row["forecast_samples"],
                "forecast_cadence_days": row["forecast_cadence_days"],
                # How far the dates were moved off the ex-date, and off how many
                # observed pairs. Absent means nothing was measured and the projected
                # date IS an ex-date — never 0, which would claim same-day settlement.
                "forecast_lag_days": row["forecast_lag_days"],
                "forecast_lag_samples": row["forecast_lag_samples"],
                # How the projected amounts were sized: `announced` (IBKR's open
                # accrual), `latest_payment` (a steady payer's newest regular payment,
                # carrying any raise), `same_payment_last_year` (a varying payer), or
                # `estimate` (Yahoo has recorded it, unpaid). None without a forecast.
                "forecast_method": row["forecast_method"],
                # The withholding the projection deducts, and where it came from:
                # `accrual`, `ibkr_measured`, `ibkr_country`, or `assumed` (the WHT
                # setting). None without a forecast — a rate nothing is deducted at
                # is not a claim worth making.
                "forecast_withholding_pct": (
                    round(float(wht_by_sec[sid][0] * 100), 2)
                    if row["forecast_method"] is not None and sid in wht_by_sec else None
                ),
                "forecast_withholding_source": (
                    wht_by_sec[sid][1]
                    if row["forecast_method"] is not None and sid in wht_by_sec else None
                ),
            })
        sec_rows.sort(key=lambda r: r["net_eur"] + r["forecast_net_eur"], reverse=True)

        return {
            "years": years,
            "year": year,
            "period": period,
            "months": months,
            "ttm_series": ttm_series,
            "ttm_coverage_start": ttm_coverage_start,
            "stack_order": stack_order,
            "securities": sec_rows,
            "total_net_eur": round(float(total_net), 2),
            "total_forecast_net_eur": round(float(total_forecast), 2),
            "ibkr_from": ibkr_from.isoformat() if ibkr_from else None,
            "base_currency": base_fx.base_currency,
            "forecast_withholding_pct": round(
                float((Decimal(1) - net_factor) * Decimal(100)), 3
            ),
            "growth": growth,
            "forward_yield": forward_yield,
            "upcoming": upcoming,
        }
