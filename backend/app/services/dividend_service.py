"""
Dividend Service
Fetches dividend ex-dates from yfinance, computes income from tax lots,
converts to EUR, and provides monthly summary data.
"""
from typing import Dict, List, Optional
from datetime import timedelta, date
from app.clock import utcnow
from decimal import Decimal
from collections import defaultdict
import logging
import random
import asyncio
import yfinance as yf

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security import Security
from app.services.yahoo_eligibility import yahoo_eligible
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.repositories.sync_run_repository import utc_iso
from app.services.currency_service import CurrencyService
from app.services.yahoo_rate_limit import is_rate_limit
from app.services.dividend_forecast import HistPayment, infer_gap_days, project_dividends

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


def _forward_basis(total: Decimal, gross_estimate: Decimal) -> str:
    """
    What the forward yield's numerator is net of: 'net', 'mixed' or 'gross_estimate'.

    Three-way for the same reason as {@link _summary_source}: a projection sized from
    dividends actually received is net of real withholding, while one sized from
    yfinance's gross per-share deducts none and so runs high. A flat 'net' on a total
    that is 7% gross claims a precision it does not have — and a yield is the figure
    most likely to be compared against a broker's own, which quotes gross.
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
        grouped: Dict = defaultdict(lambda: {"gross": Decimal("0"), "wht": Decimal("0"), "currency": None})

        for ct in cash_txns:
            security_id = conid_map.get(str(ct["conid"]))
            if not security_id:
                continue
            key = (security_id, ct["pay_date"])
            g = grouped[key]
            g["currency"] = g["currency"] or ct.get("currency")
            if ct["type"] in ("DIVIDEND", "PAYMENTINLIEU"):
                g["gross"] += ct["amount"]
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
        # Matched per security, nearest-first, one-to-one, and bounded — never by
        # amount, which cannot work when one side is gross and the other net. The
        # one-to-one part is what makes a window wider than a monthly cycle safe: an
        # IBKR payment consumes at most one estimate, so a monthly payer's earlier
        # estimates survive instead of being swallowed by the same window.
        consumed: set = set()
        candidates = sorted(
            (p for p in kept if p.source != "ibkr"),
            key=lambda p: (p.ex_date or p.pay_date),
            reverse=True,  # nearest to the pay date first
        )
        for row in sorted(ibkr_rows, key=lambda p: (p.pay_date or p.ex_date)):
            pay = row.pay_date or row.ex_date
            earliest = pay - timedelta(days=EX_TO_PAY_MAX_LAG_DAYS)
            for est in candidates:
                if id(est) in consumed or est.security_id != row.security_id:
                    continue
                ex = est.ex_date or est.pay_date
                if earliest <= ex < pay:
                    consumed.add(id(est))
                    break

        if consumed:
            kept = [p for p in kept if id(p) not in consumed]
        return kept, boundary

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

    async def _forecast_inputs(
        self, raw_payments: List, securities: Dict[int, Security], as_of: date
    ) -> tuple:
        """
        Everything ``project_dividends`` needs, assembled once.

        Split out because the same inference now has to be driven over two
        horizons — the year the caller selected (the chart) and a rolling one
        (the growth figures and the calendar, which must not change when the year
        filter does). Assembling twice would be waste; assembling in two places
        would be a second copy of these rules free to drift from this one, which
        is the failure mode this codebase keeps hitting.

        Returns ``(hist_by_sec, basis_by_sec, lots_by_sec, shares_at)``.
        """
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

        # Built from the RAW history, not the era-spliced income: a payout from
        # before we owned the share still evidences the schedule, and its
        # per-share amount still sizes the next one. Keying on realized income
        # left every recently-bought payer — TSMC, Samsung, SK Hynix, HPE, the
        # SOXQ ETF — forecasting nothing.
        fx_to_eur = await self._latest_fx_to_eur(
            {s.currency for s in securities.values() if s.currency}, as_of
        )

        # Cadence must come from ONE dated series. The same dividend is recorded
        # twice — yfinance under its ex-date, IBKR under its pay date — and the
        # two sit weeks apart, which halves the apparent gap: ASML's quarterly
        # schedule read as 74 days, 5 payouts a year instead of 4. Deduplication
        # cannot separate them, because Mastercard's ex-to-pay lag of 29 days is
        # longer than a monthly payer's whole cycle. yfinance carries the complete,
        # regular ex-date series, so where it exists it alone defines the schedule.
        #
        # But note the cost of that rule: the chosen series is trusted absolutely,
        # including the IBKR rows it then discards. When SBI's two estimate rows
        # turned out to have come from the wrong ticker, they alone projected a
        # monthly schedule for a company that does not pay one, and the real
        # payment was skipped. Hence forecast_samples on the response and the
        # provenance check in SchedulerService — the rule stays, but a thin or
        # suspect inference now says so instead of looking like any other.
        per_share_rows = defaultdict(list)
        for p in raw_payments:
            if p.amount_per_share is not None:
                per_share_rows[p.security_id].append(p)
        schedule_source = {
            sid: rows for sid, rows in per_share_rows.items() if len(rows) >= 2
        }

        hist_by_sec: Dict[int, List[HistPayment]] = defaultdict(list)
        basis_by_sec: Dict[int, str] = {}
        for p in raw_payments:
            scheduled = schedule_source.get(p.security_id)
            if scheduled is not None and p.amount_per_share is None:
                continue  # a second record of a dividend already counted
            on_date = p.pay_date or p.ex_date
            if on_date is None or on_date > as_of:
                continue
            sec = securities.get(p.security_id)
            if sec is None:
                continue
            # Prefer a net-of-withholding per-share figure derived from what
            # actually landed; fall back to the gross per-share yfinance
            # publishes. Never mix the two inside one security — that would
            # average a gross figure against a net one.
            #
            # Only an IBKR row is "what actually landed". A yfinance_estimate row
            # with shares held also has gross > 0, but compute_dividend_income writes
            # its net as gross with zero withholding — so dividing *that* by the
            # shares gives the gross per-share figure back, and stamping it "net"
            # labelled a projection that deducts no withholding as one that did.
            # SK Hynix read `net` with no IBKR payout on record.
            net_ps = None
            gross_ps = None
            if self._is_income(p):
                # IBKR rows store a 0 sentinel in shares_held, so fall back to
                # the holding the tax lots show for that date.
                shares = (p.shares_held if (p.shares_held and p.shares_held > 0)
                          else shares_at(p.security_id, on_date))
                if shares > 0:
                    per_share = self._net_eur(p) / shares
                    if p.source == "ibkr":
                        net_ps = per_share
                    else:
                        # The same figure the row always contributed — the EUR
                        # amount converted at its ex-date is a better gross per
                        # share than amount_per_share × one recent rate — under
                        # the label it deserves.
                        gross_ps = per_share
            if gross_ps is None and p.amount_per_share and p.amount_per_share > 0:
                rate = fx_to_eur.get(sec.currency or "EUR")
                if rate is not None:
                    gross_ps = p.amount_per_share * rate
            if net_ps is not None:
                basis_by_sec[p.security_id] = "net"
            hist_by_sec[p.security_id].append(HistPayment(
                on_date=on_date, per_share_eur=(net_ps, gross_ps),
            ))

        # Collapse each security to one basis now that we know whether any
        # realized figure exists for it.
        for sid, entries in hist_by_sec.items():
            prefer_net = basis_by_sec.get(sid) == "net"
            hist_by_sec[sid] = [
                HistPayment(
                    on_date=e.on_date,
                    per_share_eur=(e.per_share_eur[0] if prefer_net
                                   else e.per_share_eur[1]),
                )
                for e in entries
            ]
            if not prefer_net:
                basis_by_sec[sid] = "gross_estimate"

        return hist_by_sec, basis_by_sec, lots_by_sec, shares_at

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

        def _in_window(d: date) -> bool:
            return (win_start is None or d >= win_start) and (win_end is None or d <= win_end)

        def _new_row() -> Dict:
            return {
                "payouts": 0, "gross": Decimal("0"), "wht": Decimal("0"),
                "net": Decimal("0"), "forecast_payouts": 0,
                "forecast_net": Decimal("0"), "sources": set(),
                "forecast_basis": None,
                "forecast_samples": None, "forecast_cadence_days": None,
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

        for p in all_payments:
            on_date = p.pay_date or p.ex_date
            if not _in_window(on_date):
                continue
            net = base_fx.convert(self._net_eur(p), on_date)
            row = by_sec.setdefault(p.security_id, _new_row())
            row["payouts"] += 1
            row["net"] += net
            row["gross"] += base_fx.convert(p.gross_amount_eur or Decimal("0"), on_date)
            row["wht"] += base_fx.convert(p.withholding_tax_eur or Decimal("0"), on_date)
            row["sources"].add("ibkr" if p.source == "ibkr" else "estimate")
            monthly_actual[on_date.strftime("%Y-%m")][_symbol(p.security_id)] += net
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
        next_12m = Decimal("0")
        # The same 365-day total, split per security, for the forward yields. Kept
        # beside the aggregate rather than derived from the per-security table
        # afterwards, because a table row is limited to the SELECTED window while a
        # yield must always describe the next twelve months — with ?year=2026 asked
        # in August, `forecast_net` covers Aug-Dec and would read 5/12 of the truth.
        next_12m_by_sec: Dict[int, Decimal] = defaultdict(Decimal)
        next_pay: Dict[int, date] = {}
        next_12m_end = as_of + timedelta(days=365)

        if include_forecast:
            hist_by_sec, basis_by_sec, lots_by_sec, shares_at = \
                await self._forecast_inputs(raw_payments, securities, as_of)

            # Far enough to complete next calendar year, so the year comparison
            # never shows a truncated bar; further still if the caller asked for
            # a year beyond that.
            horizon_start = as_of + timedelta(days=1)
            horizon_end = max(win_end or date.min, date(as_of.year + 1, 12, 31))

            # The CHART's reach is unchanged by that widening: without a selected
            # year it still stops at the end of the current year. Projecting
            # further for the growth figures must not quietly stretch the all-time
            # chart into next year — that alone tripled its forecast total.
            chart_end = win_end or date(as_of.year, 12, 31)

            for sid in lots_by_sec:
                qty = shares_at(sid, as_of)
                if qty <= 0:
                    continue
                projected = project_dividends(hist_by_sec.get(sid, []), qty,
                                              horizon_start, horizon_end,
                                              as_of=as_of)
                if not projected:
                    continue

                symbol = _symbol(sid)
                basis = basis_by_sec.get(sid)
                next_pay[sid] = min(fp.on_date for fp in projected)

                for fp in projected:
                    amt = base_fx.convert(fp.net_eur, fp.on_date)
                    annual_forecast[fp.on_date.year] += amt
                    if fp.on_date <= next_12m_end:
                        next_12m += amt
                        next_12m_by_sec[sid] += amt
                        upcoming.append({
                            "date": fp.on_date.isoformat(),
                            "security_id": sid,
                            "symbol": symbol,
                            "net_eur": round(float(amt), 2),
                            "basis": basis,
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
                for fp in in_window:
                    amt = base_fx.convert(fp.net_eur, fp.on_date)
                    row = by_sec[sid]
                    row["forecast_payouts"] += 1
                    row["forecast_net"] += amt
                    monthly_forecast[fp.on_date.strftime("%Y-%m")][symbol] += amt
                    total_forecast += amt

        upcoming.sort(key=lambda u: (u["date"], u["symbol"]))

        # ---- Growth -------------------------------------------------------------
        # Deliberately computed from the UNWINDOWED history: with year=2026
        # selected the response carries no 2025 months, so no client could derive
        # any of this. Doing it here also keeps the era splice and the per-date FX
        # projection in one place instead of growing a second implementation.
        annual_actual: Dict[int, Decimal] = defaultdict(Decimal)
        month_actual_all: Dict[str, Decimal] = defaultdict(Decimal)
        for p in all_payments:
            d = p.pay_date or p.ex_date
            value = base_fx.convert(self._net_eur(p), d)
            annual_actual[d.year] += value
            month_actual_all[d.strftime("%Y-%m")] += value

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
            # deducts no withholding and so runs a little high. Reported as a share of
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
        else:
            keys = sorted(set(monthly_actual) | set(monthly_forecast))
            months_axis = []
            if keys:
                y, m = (int(x) for x in keys[0].split("-"))
                last_y, last_m = (int(x) for x in keys[-1].split("-"))
                while (y, m) <= (last_y, last_m):
                    months_axis.append(f"{y:04d}-{m:02d}")
                    y, m = (y, m + 1) if m < 12 else (y + 1, 1)

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
                # exists — the latter ignores withholding and so runs a little high.
                "forecast_basis": row["forecast_basis"],
                # How thin the projection's inference is: how many dated payments
                # defined the schedule, and the median gap it settled on. Two
                # samples is a guess with a schedule attached.
                "forecast_samples": row["forecast_samples"],
                "forecast_cadence_days": row["forecast_cadence_days"],
            })
        sec_rows.sort(key=lambda r: r["net_eur"] + r["forecast_net_eur"], reverse=True)

        return {
            "years": years,
            "year": year,
            "months": months,
            "securities": sec_rows,
            "total_net_eur": round(float(total_net), 2),
            "total_forecast_net_eur": round(float(total_forecast), 2),
            "ibkr_from": ibkr_from.isoformat() if ibkr_from else None,
            "base_currency": base_fx.base_currency,
            "growth": growth,
            "forward_yield": forward_yield,
            "upcoming": upcoming,
        }
