"""
Tax Service
Assembles a per-year tax report from authoritative IBKR data, framed for Swiss
filing:
  * Dividend income with foreign withholding tax (DA-1 reclaim) — real figures
    from <CashTransactions> when available, else yfinance-estimated gross — and, per
    withholding country, the part DA-1 can credit (`swiss_treaty_rates`).
  * Realized capital gains per SELL trade (Switzerland doesn't tax private
    capital gains, but the figures are provided for completeness / other regimes).
  * A current holdings snapshot as an indicative wealth-tax (Steuerwert) base, with
    the IBKR cash balance at the same date beside it.
  * For the current year only, the dividends and withholding expected through
    31 December -- the dividend forecast's own figures, not a second forecast.
  * Three ESTV Kreisschreiben 36 securities-dealer indicators (`trader_criteria`).

All monetary values are projected into the configured base currency.
"""
import csv
import io
import logging
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import IBKR, tax_exempt_accounts
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade, buy_sign, is_sale_side, sale_sign, uncancelled_sales
from app.models.dividend_payment import DividendPayment
from app.repositories.dividend_repository import IBKR_SOURCE, YFINANCE_SOURCE
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.services.cash_service import CashService
from app.services.currency_service import CurrencyService
from app.services.dividend_service import DividendService
from app.services.native_amounts import NativeToBase, cash_flow_in_base
from app.services.percentages import share_pct
from app.services.realized_trades import realized_from_sell_trades
from app.services.portfolio_service import PortfolioService
from app.services.swiss_treaty_rates import (
    creditable_split,
    residual_rate_pct,
    withholding_country,
)
from app.services.trader_criteria import (
    lowest_balance,
    sales_by_day,
    short_held_counts,
    turnover_multiple,
)
from app.clock import utc_today

logger = logging.getLogger(__name__)


def _blank(value):
    """A CSV cell for a figure that may be unknown: blank, never 0.00."""
    return "" if value is None else value


class TaxService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.currency_service = CurrencyService(db)

    async def get_tax_report(self, year: int) -> Dict:
        start = date(year, 1, 1)
        end = date(year, 12, 31)

        # Resolved once and applied at three call sites -- the dividend/DA-1 select,
        # the realized-gains select and the Steuerwert snapshot. A second copy of
        # this predicate is this codebase's opening warning, and here the two copies
        # would disagree on a tax return rather than on a dashboard.
        exempt_accounts = await tax_exempt_accounts(self.db)

        portfolio = PortfolioService(self.db)
        base_fx = await portfolio._load_base_fx()

        # --- Dividend income (era-spliced, like the dividend card) ---
        # The boundary is the globally first IBKR payment: estimates strictly before
        # it, authoritative rows from it on, then windowed to the year below. A
        # per-year boolean dropped every estimate inside the boundary year — real
        # January income vanished from a filing aid that then badged itself
        # authoritative — while a per-year boundary would resurrect the
        # ex-date/pay-date double-count in later years (yfinance stores a dividend
        # under its ex-date, IBKR under its pay date, weeks apart). The two sources
        # are still never summed for the same period.
        # A tax-exempt account is excluded here, and this guard can never fire
        # today: the finpension importer books 3a distributions to `cash_flows`
        # rather than `dividend_payments`, so there is no row to exclude. That is
        # exactly why it is written -- a guard that cannot fire is the one you want
        # on a DA-1 bucket keyed on `isin[:2]`, because both Swisscanto ISINs begin
        # `CH` and would land in the one bucket that is definitionally not a
        # foreign-withholding reclaim. It also gates `dividend_income` in the same
        # clause, so the two cannot come apart.
        stmt = (
            select(DividendPayment, Security)
            .join(Security, DividendPayment.security_id == Security.id)
            .where(
                DividendPayment.gross_amount_eur.isnot(None),
                *([Security.account.notin_(exempt_accounts)]
                  if exempt_accounts else []),
            )
        )
        all_rows = (await self.db.execute(stmt)).all()
        kept, ibkr_from = DividendService._splice_by_era([dp for dp, _ in all_rows])
        kept_ids = {p.id for p in kept}
        rows = [(dp, sec) for dp, sec in all_rows if dp.id in kept_ids]

        dividend_income: List[Dict] = []
        sources_seen: set = set()
        div_gross = div_wht = div_net = Decimal("0")
        estimated_rows = 0
        # DA-1 is filed per source country, so accumulate a per-country breakdown as we go.
        by_country: Dict[str, Dict] = {}
        for dp, sec in rows:
            on_date = dp.pay_date or dp.ex_date
            if on_date is None or on_date < start or on_date > end:
                continue
            # yfinance carries a security's whole dividend history, so ex-dates
            # from before it was bought are stored as zero rows. They are not
            # income and must not pad a filing aid: 70 of the 96 rows the 2025
            # report listed were 0.00.
            if not DividendService._is_income(dp):
                continue
            gross_e = dp.gross_amount_eur or Decimal("0")
            wht_e = dp.withholding_tax_eur or Decimal("0")
            net_e = DividendService._net_eur(dp)
            gross = base_fx.convert(gross_e, on_date)
            wht = base_fx.convert(wht_e, on_date)
            net = base_fx.convert(net_e, on_date)
            row_source = IBKR_SOURCE if dp.source == IBKR_SOURCE else YFINANCE_SOURCE
            # An estimate has no withholding on record: `compute_dividend_income` stores
            # 0 because yfinance carries none, so its 0 means "not known", never "nothing
            # withheld". It is absent here, and the withholding total is the RECORDED
            # withholding only — the count below says how many rows it leaves out. The
            # net keeps the gross fallback (`_net_eur`), so real income is not dropped,
            # and is counted the same way. Deliberately NOT `IncomeSplit`, which every
            # other reader uses (issue #61): its net is gross less an ASSUMED withholding,
            # and on a filing aid gross minus net would publish that assumption as a
            # withholding figure this report refuses to state (docs/tax.md).
            estimated = row_source == YFINANCE_SOURCE
            if estimated:
                estimated_rows += 1
                wht = Decimal("0")
            div_gross += gross
            div_wht += wht
            div_net += net
            sources_seen.add(row_source)
            dividend_income.append({
                "symbol": sec.symbol,
                "description": sec.description,
                "isin": sec.isin,
                "pay_date": on_date.isoformat(),
                "gross": round(float(gross), 2),
                "withholding": None if estimated else round(float(wht), 2),
                "net": round(float(net), 2),
                "source": row_source,
            })

            # The country IBKR's withholding line names, else the ISIN's first two
            # letters. The prefix is the issuer's domicile, which is not the withholding
            # country for an ADR or some funds (GitHub issue #62).
            country = withholding_country(dp.withholding_country, sec.isin) or "??"
            bucket = by_country.setdefault(country, {
                "country": country,
                "gross": Decimal("0"), "withholding": Decimal("0"), "net": Decimal("0"),
                # The recorded payments' gross alone, for the rate: an estimate's gross
                # beside no withholding would dilute it.
                "recorded_gross": Decimal("0"),
                "creditable": Decimal("0"), "excess": Decimal("0"), "unsplit": 0,
                "symbols": set(), "payments": 0, "estimated_payments": 0,
            })
            bucket["gross"] += gross
            bucket["withholding"] += wht
            bucket["net"] += net
            bucket["payments"] += 1
            if estimated:
                bucket["estimated_payments"] += 1
            else:
                # DA-1 credits only the treaty residual rate; withholding above it is
                # reclaimed from the source country. Split per payment, since
                # min(withheld, gross x rate) does not add across payments.
                bucket["recorded_gross"] += gross
                split = creditable_split(gross, wht, residual_rate_pct(country))
                if split is None:
                    bucket["unsplit"] += 1
                else:
                    bucket["creditable"] += split[0]
                    bucket["excess"] += split[1]
            if sec.symbol:
                bucket["symbols"].add(sec.symbol)

        dividend_income.sort(key=lambda d: d["pay_date"])

        def _split_part(b: Dict, key: str) -> Optional[float]:
            # Absent, not 0.00, when there is nothing recorded to split or a payment
            # withheld with no treaty rate in the table: a guessed split on a filing aid
            # is worse than none.
            if b["estimated_payments"] == b["payments"] or b["unsplit"]:
                return None
            return round(float(b[key]), 2)

        def _residual(country: str) -> Optional[float]:
            rate = residual_rate_pct(country)
            return None if rate is None else float(rate)

        dividend_by_country = [
            {
                "country": b["country"],
                "gross": round(float(b["gross"]), 2),
                # Absent when every payment from the country is an estimate: there is
                # no withholding on record to report, and 0.00 would read as none.
                "withholding": (
                    None if b["estimated_payments"] == b["payments"]
                    else round(float(b["withholding"]), 2)
                ),
                "net": round(float(b["net"]), 2),
                # Withheld as a share of the recorded gross, and what the treaty allows.
                "rate_pct": share_pct(b["withholding"], b["recorded_gross"]),
                "residual_pct": _residual(b["country"]),
                "creditable": _split_part(b, "creditable"),
                "excess": _split_part(b, "excess"),
                "positions": len(b["symbols"]),
                # DA-1 is filed per country, so which countries' withholding is
                # incomplete is said per country, not once for the year.
                "payments": b["payments"],
                "estimated_payments": b["estimated_payments"],
            }
            # Sorted on the recorded amount, so an all-estimate country sorts as nothing
            # recorded rather than failing to compare.
            for b in sorted(by_country.values(), key=lambda b: b["withholding"], reverse=True)
        ]

        # --- Realized capital gains (per SELL trade) ---
        # Filtered on `Trade.account`, NOT on the joined security. The join is an
        # OUTER one because `Trade.security_id` is nullable by design (a fully-sold
        # security is absent from OpenPositions), so a Security-side clause would
        # silently drop every unlinked IBKR trade along with the 3a ones.
        trade_rows = (await self.db.execute(
            select(Trade, Security)
            .join(Security, Trade.security_id == Security.id, isouter=True)
            .where(and_(
                Trade.trade_date >= start,
                Trade.trade_date <= end,
                *([Trade.account.notin_(exempt_accounts)]
                  if exempt_accounts else []),
            ))
            .order_by(Trade.trade_date.asc())
        )).all()

        realized_gains: List[Dict] = []
        r_proceeds = r_cost = r_gain = Decimal("0")
        warnings: List[str] = []
        # Trade money is in the trade's own currency, so it takes the native->EUR->base
        # two-step `NativeToBase` owns: one rate lookup per (currency, date), and a
        # sale already in the base currency is its own amount rather than a round trip
        # through EUR (GitHub issue #46 — this loop was a hand-written copy of it). The
        # loop itself is `realized_from_sell_trades`, shared with the summary and the
        # closed positions (issue #58). This report's policy: list every converted
        # sale-side row of the year — a CANCELSELL as a row of negated figures, so the
        # totals net a busted sale out — and omit and warn per unconvertible one.
        sec_of = {id(t): sec for t, sec in trade_rows}
        # Shared with the turnover's purchases below, so a (currency, date) is looked up once.
        to_base = NativeToBase(self.currency_service, base_fx)
        realized = await realized_from_sell_trades([t for t, _ in trade_rows], to_base)
        # The conversion returned the *unconverted* amount on failure until
        # 2026-07-30, reporting a foreign figure as base currency — a TWD sale ~35x
        # high in a filing aid badged realized_source='trades', the badge that
        # means authoritative. `None` is the only answer to a missing rate now, and
        # the row is omitted: the raw foreign amount would look plausible.
        trades_skipped = len(realized.unconvertible)
        skipped_currencies = {t.currency or "?" for t in realized.unconvertible}
        for row in realized.rows:
            t, sec = row.trade, sec_of[id(row.trade)]
            proceeds, gain = row.proceeds, row.gain
            cost = proceeds - gain
            r_proceeds += proceeds
            r_cost += cost
            r_gain += gain
            realized_gains.append({
                "symbol": t.symbol or (sec.symbol if sec else None),
                # The holding's identity, for its logo; None for an unlinked trade.
                "security_id": t.security_id,
                "isin": sec.isin if sec else None,
                "trade_date": t.trade_date.isoformat(),
                # Negative on a cancellation, like every other figure on its row.
                "quantity": (
                    float(abs(t.quantity)) * sale_sign(t) if t.quantity is not None else None
                ),
                "proceeds": round(float(proceeds), 2),
                "cost_basis": round(float(cost), 2),
                "gain_loss": round(float(gain), 2),
            })

        realized_source = "trades"
        if not realized_gains:
            # No authoritative SELL trade in this year — either <Trades> hasn't been
            # ingested yet, or the statement period doesn't reach back this far. Fall
            # back to the same market-price approximation over closed lots that the
            # portfolio view uses, so the two never disagree; flagged so the UI can
            # label it an estimate (mirrors dividend_source).
            realized_source = "closed_lot_estimate"
            for row in await portfolio.realized_rows_from_closed_lots(
                base_fx, start, end, exclude_accounts=exempt_accounts
            ):
                proceeds, cost = row["proceeds"], row["cost_basis"]
                gain = proceeds - cost
                r_proceeds += proceeds
                r_cost += cost
                r_gain += gain
                realized_gains.append({
                    "symbol": row["symbol"],
                    "security_id": row["security_id"],
                    "isin": row["isin"],
                    "trade_date": row["close_date"].isoformat(),
                    "quantity": float(abs(row["quantity"])) if row["quantity"] is not None else None,
                    "proceeds": round(float(proceeds), 2),
                    "cost_basis": round(float(cost), 2),
                    "gain_loss": round(float(gain), 2),
                })
            # Read the latch right after the loop: per-run state, like
            # `last_snapshot_skipped` below. A lot with no price near its close date is
            # missing from the rows above, so the year's realized totals are partial.
            unpriced_lots = sum(
                len(d) for d in portfolio.last_realized_skipped.values()
            )
            if unpriced_lots:
                warnings.append(
                    f"{unpriced_lots} closed lot(s) could not be priced at their close "
                    f"date and are omitted from the realized gains estimate, so the "
                    f"realized totals below cover only part of the year's sales."
                )

        if trades_skipped:
            currencies = ", ".join(sorted(skipped_currencies))
            if realized_source == "trades":
                warnings.append(
                    f"{trades_skipped} SELL trade(s) omitted from realized gains: no "
                    f"{currencies} exchange rate for the trade date. The realized "
                    f"totals below understate the year."
                )
            else:
                # Every SELL was unconvertible, so the closed-lot approximation
                # took over. It reads cost_basis_eur, which was converted at
                # ingest, so it needs no trade-date rate — a genuinely independent
                # derivation rather than a hole, but the badge must still say so.
                warnings.append(
                    f"All {trades_skipped} SELL trade(s) were unconvertible (no "
                    f"{currencies} rate for the trade date), so realized gains fall "
                    f"back to the closed-lot approximation."
                )

        # --- Year-end holdings (wealth-tax base / Steuerwert) ---
        # Switzerland values wealth at 31 December, so a past year must be reconstructed
        # at that date rather than reported as today's positions. For the current year
        # (no 31 Dec yet) today's snapshot is the right answer.
        today = utc_today()
        as_of = end if end < today else today
        holdings: List[Dict] = []
        holdings_total = Decimal("0")
        holdings_failed = False
        try:
            # `exclude_accounts` deliberately breaks this helper's documented
            # promise that it uses the same window as `_calculate_daily_value`, so
            # the wealth-tax base and the portfolio timeline no longer agree. That
            # is the point: 3a capital is outside the Steuerwert entirely, taxed
            # instead on withdrawal at a separate reduced rate.
            for row in await portfolio.holdings_snapshot_as_of(
                base_fx, as_of, exclude_accounts=exempt_accounts
            ):
                mv = row["market_value"]
                holdings_total += mv
                holdings.append({
                    "symbol": row["symbol"],
                    "security_id": row["security_id"],
                    "isin": row["isin"],
                    "quantity": float(row["quantity"]),
                    "market_value": round(float(mv), 2),
                    "cost_basis": round(float(row["cost_basis"]), 2),
                })
        except Exception as e:
            # A bare `pass` here served a Steuerwert of 0.00 as HTTP 200 with the
            # note below still claiming the holdings were valued at that date's
            # closes — nothing separated "no holdings" from "the snapshot raised",
            # and the exception never reached the logs either.
            logger.error(f"Holdings snapshot for {as_of} failed: {e}", exc_info=True)
            holdings_failed = True
            holdings = []
            holdings_total = Decimal("0")
            warnings.append(
                f"The {as_of.isoformat()} holdings snapshot could not be built, so the "
                f"wealth-tax base is unavailable — it is not zero."
            )
        else:
            # The PARTIAL case, which had no reporting at all until 2026-08-05. A
            # snapshot that raises is handled above and yields None rather than a
            # figure; a snapshot that quietly returned fewer rows yielded a plausible
            # number that reads as the complete wealth-tax base. That asymmetry is the
            # one this codebase keeps paying for — a missing figure reads as a fault, a
            # partial one reads as an answer — and it matters most here, because this
            # figure goes on a tax return.
            #
            # Read from the latch immediately after the call: it is per-run state, so
            # anything between the two would risk reporting another date's snapshot.
            skipped = portfolio.last_snapshot_skipped
            if skipped:
                warnings.append(
                    f"The {as_of.isoformat()} holdings snapshot omits "
                    f"{len(skipped)} security(ies) that could not be valued that day "
                    f"({', '.join(skipped)}), so the wealth-tax base below understates "
                    f"by their value. Fix the ticker mapping or import prices for that "
                    f"date, then re-run."
                )

        # --- Cash at the same date (GitHub issue #63) ------------------------------
        # Declared wealth is securities *and* cash, and a negative balance is a margin
        # debit, which is debt. Scoped to IBKR: pillar 3a cash is outside the Steuerwert
        # exactly as its holdings are. Absent when unknown -- no ledger at all, or a trade
        # with no exchange rate -- never 0.00, which would read as "no cash".
        cash_balance: Optional[Decimal] = None
        # One instance for this line and the margin indicator below, so the ledgers are
        # read and converted once.
        cash_service = CashService(self.db)
        try:
            cash = await cash_service.uninvested_cash(base_fx, as_of, account=IBKR)
        except Exception as e:
            logger.error(f"Cash balance for {as_of} failed: {e}", exc_info=True)
            warnings.append(
                f"The {as_of.isoformat()} cash balance could not be computed, so it is "
                f"absent — it is not zero."
            )
        else:
            cash_balance = cash.balance
            if cash.unconverted:
                warnings.append(
                    f"{cash.unconverted} trade(s) have no exchange rate for their trade "
                    f"date, so the {as_of.isoformat()} cash balance is unknown."
                )

        # --- Pillar 3a: excluded above, and reported here instead -----------------
        # Its capital is outside the wealth-tax base and its income is not taxable,
        # so none of the three sections above may contain it. What *is* a real tax
        # figure is the year's contributions: they are deductible from taxable
        # income, up to a federal cap that depends on whether you have a pension
        # fund. Deposits only -- a `TRANSFER_IN` is capital already deducted in an
        # earlier year, and counting it would overstate the deduction, which is the
        # error direction that costs money at an audit.
        exempt_contributions = Decimal("0")
        exempt_holdings_total = Decimal("0")
        exempt_holdings_failed = False
        has_exempt = False
        if exempt_accounts:
            rows = (await self.db.execute(
                select(CashFlow).where(and_(
                    CashFlow.account.in_(exempt_accounts),
                    CashFlow.flow_type == DEPOSIT_WITHDRAW,
                    CashFlow.flow_date >= start,
                    CashFlow.flow_date <= end,
                ))
            )).scalars().all()
            has_exempt = True
            for flow in rows:
                converted = cash_flow_in_base(flow, base_fx)
                if converted is not None:
                    exempt_contributions += converted
            # Stated so the reader can see the assets exist and are deliberately
            # absent from the Steuerwert, rather than wondering where they went.
            # Guarded like the Steuerwert's own snapshot (GitHub issue #63): unguarded,
            # the same failure that makes that figure None here answered 500 for the
            # whole report, and its partial case was never read.
            try:
                for row in await portfolio.holdings_snapshot_as_of(
                    base_fx, as_of, only_accounts=exempt_accounts
                ):
                    exempt_holdings_total += row["market_value"]
            except Exception as e:
                logger.error(
                    f"Pillar 3a holdings snapshot for {as_of} failed: {e}", exc_info=True
                )
                exempt_holdings_failed = True
                warnings.append(
                    f"The {as_of.isoformat()} pillar 3a holdings could not be valued, so "
                    f"their value is unavailable — it is not zero."
                )
            else:
                # The latch, read right after its own call -- the Steuerwert's reading
                # above is for the other snapshot.
                skipped_3a = portfolio.last_snapshot_skipped
                if skipped_3a:
                    warnings.append(
                        f"The {as_of.isoformat()} pillar 3a holdings omit "
                        f"{len(skipped_3a)} security(ies) that could not be valued that "
                        f"day ({', '.join(skipped_3a)}), so the 3a assets figure "
                        f"understates by their value."
                    )
            # The why lives in `pillar3a.note` (and the CSV), not in `warnings`: it
            # held on every report with a 3a account, and a warning that is always
            # present teaches the reader to skip the banner (owner, 2026-10-14).

        # Recorded withholding only, absent when every row is an estimate (see the loop).
        withholding_total: Optional[Decimal] = (
            None if dividend_income and estimated_rows == len(dividend_income) else div_wht
        )

        # --- The current year through 31 December (GitHub issue #77) -----------------
        # Absent for a past year, whose figures are all in. Never a 500: the projection
        # is a preview beside the filing figures, so its failure is a warning.
        projected: Optional[Dict] = None
        if year == today.year:
            try:
                projected = await self._projected_to_year_end(
                    year, today, base_fx, div_gross, withholding_total
                )
            except Exception as e:
                logger.error(f"Dividend projection for {year} failed: {e}", exc_info=True)
                warnings.append(
                    f"The dividends expected through {end.isoformat()} could not be "
                    f"computed, so the year-end projection is absent."
                )

        # --- Securities-dealer indicators (ESTV KS 36, GitHub issue #78) -------------
        trader_criteria: Optional[Dict] = None
        try:
            trader_criteria = await self._trader_criteria(
                year, as_of, base_fx, to_base, portfolio, cash_service, exempt_accounts,
                [t for t, _ in trade_rows], realized_source, bool(realized_gains),
                r_proceeds, trades_skipped, warnings,
            )
        except Exception as e:
            logger.error(f"Securities-dealer indicators for {year} failed: {e}", exc_info=True)
            warnings.append(
                f"The securities-dealer indicators for {year} could not be computed, so "
                f"they are absent."
            )

        # The label reflects what actually contributed inside the year window:
        # "mixed" is the boundary year, where estimates cover the weeks before the
        # cash-transaction ledger began mid-year.
        if sources_seen == {IBKR_SOURCE}:
            dividend_source = IBKR_SOURCE
        elif IBKR_SOURCE in sources_seen:
            dividend_source = "mixed"
        else:
            dividend_source = YFINANCE_SOURCE

        return {
            "year": year,
            "base_currency": base_fx.base_currency,
            "dividend_source": dividend_source,
            "dividend_ibkr_from": ibkr_from.isoformat() if ibkr_from else None,
            "dividend_income": dividend_income,
            "dividend_by_country": dividend_by_country,
            "dividend_country_note": (
                "Country from IBKR's withholding line, else the ISIN prefix."
            ),
            "dividend_totals": {
                "gross": round(float(div_gross), 2),
                # Recorded withholding only; `estimated_rows` is what it leaves out, and
                # what `gross` and `net` include. Absent when every row is an estimate.
                "withholding": (
                    None if withholding_total is None else round(float(withholding_total), 2)
                ),
                "net": round(float(div_net), 2),
                "estimated_rows": estimated_rows,
            },
            # The current year only: received plus expected through 31 December, `est.`.
            "projected": projected,
            "realized_gains": realized_gains,
            "realized_source": realized_source,
            "realized_totals": {
                "proceeds": round(float(r_proceeds), 2),
                "cost_basis": round(float(r_cost), 2),
                "gain_loss": round(float(r_gain), 2),
            },
            "holdings_snapshot": holdings,
            # None, not 0.00: a failed snapshot has no total, and a zero would be
            # read as a computed wealth-tax base.
            "holdings_snapshot_total": None if holdings_failed else round(float(holdings_total), 2),
            "holdings_snapshot_error": holdings_failed,
            "holdings_as_of": as_of.isoformat(),
            # IBKR cash at `holdings_as_of`, signed; None when unknown (see above).
            "cash_balance": None if cash_balance is None else round(float(cash_balance), 2),
            "holdings_snapshot_note": (
                f"The {as_of.isoformat()} holdings snapshot could not be built; no "
                f"wealth-tax base is reported for this year."
                if holdings_failed else
                f"Holdings as at {as_of.isoformat()}, valued at that date's closing prices"
                + ("." if as_of == end else " (current year — 31 December has not occurred yet).")
                + " Positions whose price could not be resolved near that date are omitted."
            ),
            "pillar3a": {
                "tracked": has_exempt,
                "accounts": exempt_accounts if has_exempt else [],
                "contributions": round(float(exempt_contributions), 2),
                # None when the snapshot raised: a missing figure, not a zero one.
                "holdings_value": (
                    None if exempt_holdings_failed
                    else round(float(exempt_holdings_total), 2)
                ),
                "note": (
                    "Pillar 3a contributions are deductible from taxable income. "
                    "The assets themselves are excluded from every section above: "
                    "3a capital is not part of the Steuerwert and its income is not "
                    "taxable, being taxed on withdrawal at a separate reduced rate. "
                    "Check the year's federal cap — it differs depending on whether "
                    "you are affiliated to a pension fund."
                ),
            } if has_exempt else None,
            "trader_criteria": trader_criteria,
            "warnings": warnings,
        }

    async def _projected_to_year_end(
        self,
        year: int,
        today: date,
        base_fx,
        received_gross: Decimal,
        received_withholding: Optional[Decimal],
    ) -> Dict:
        """
        The year's dividends received so far plus those expected through 31 December.

        The expected part is the dividend breakdown's own year row (`growth.annual`):
        `forecast_net_eur` and `forecast_withholding_eur`, which are in whatever base
        `base_fx` projects -- this report's. Reading it rather than projecting here is
        the point: a second forecast would be a copy of the cadence, the accruals and the
        withholding ladder, waiting to disagree with the Dividends tab. The forecast is
        net of each security's ladder rate and its withholding is net x r / (1 - r), so
        the expected gross is their sum. Received is this report's own gross and recorded
        withholding; the withholding through year end is absent when none is recorded.
        """
        breakdown = await DividendService(self.db).get_dividend_breakdown(
            year=year, as_of=today, base_fx=base_fx
        )
        row = next((r for r in breakdown["growth"]["annual"] if r["year"] == year), None)
        expected_net = Decimal(str(row["forecast_net_eur"])) if row else Decimal("0")
        expected_wht = Decimal(str(row["forecast_withholding_eur"])) if row else Decimal("0")
        expected_gross = expected_net + expected_wht
        return {
            "through": date(year, 12, 31).isoformat(),
            "expected_gross": round(float(expected_gross), 2),
            "expected_withholding": round(float(expected_wht), 2),
            "gross": round(float(received_gross + expected_gross), 2),
            "withholding": (
                None if received_withholding is None
                else round(float(received_withholding + expected_wht), 2)
            ),
        }

    async def _trader_criteria(
        self,
        year: int,
        as_of: date,
        base_fx,
        to_base: NativeToBase,
        portfolio: PortfolioService,
        cash: CashService,
        exempt_accounts: List[str],
        trades: List[Trade],
        realized_source: str,
        has_realized_rows: bool,
        sales_proceeds: Decimal,
        sales_unconvertible: int,
        warnings: List[str],
    ) -> Dict:
        """
        The three KS 36 indicators for `year` (definitions in `trader_criteria.py` and
        docs/tax.md). `trades` are the year's trades outside the tax-exempt accounts --
        the realized section's rows -- and `sales_proceeds` that section's proceeds, of
        which `sales_unconvertible` sales had to be left out.

        Each figure is absent, never a reassuring default, when the record cannot show
        it. The first test is coverage: a year the IBKR cash record does not reach (the
        holdings were at another broker) has no sales or margin on record, so every
        indicator is absent rather than "no".
        """
        start = date(year, 1, 1)
        opening_on = date(year - 1, 12, 31)
        criteria: Dict = {
            "opening_as_of": opening_on.isoformat(),
            "opening_securities": None,
            "opening_cash": None,
            "purchases": None,
            "sales_proceeds": None,
            "turnover_multiple": None,
            "turnover_estimated": False,
            "sales": None,
            "short_held_sales": None,
            "sales_holding_unknown": 0,
            "margin_used": None,
            "min_cash_balance": None,
        }

        events = await cash.balance_events(base_fx, account=IBKR)
        lowest = lowest_balance(events, start, as_of)
        if lowest is None:
            return criteria

        # Margin: did IBKR cash close a day of the year below zero. Unknown while a trade
        # has no rate -- the balance is short by the whole trade (CashService), and the
        # year-end cash line's warning already names the count.
        if not await cash.unconverted_trades(base_fx, account=IBKR):
            low = round(lowest, 2)
            criteria["min_cash_balance"] = float(low)
            criteria["margin_used"] = low < 0

        # Sales, counted on IBKR's own executions; a cancelled sale is no sale.
        if any(is_sale_side(t) for t in trades):
            sales = sales_by_day(uncancelled_sales(trades))
            short, unknown = short_held_counts(
                sales, await self._lots_closed_by_sales(sales, exempt_accounts)
            )
            criteria["sales"] = len(sales)
            criteria["sales_holding_unknown"] = unknown
            # Absent when no sale's holding period is on record at all.
            criteria["short_held_sales"] = None if sales and unknown == len(sales) else short
        elif not has_realized_rows:
            criteria["sales"] = 0
            criteria["short_held_sales"] = 0
        # Otherwise the closed-lot estimate found disposals the trade ledger does not
        # hold, so the count is not on record and stays absent.

        # Turnover, the circular's test: purchases plus sales over the securities and
        # cash at the prior 31 December. Purchases are summed as stored, converted like
        # the realized section: a BUY's proceeds are negative and its cancellation's the
        # same amount positive, so a busted purchase nets out.
        purchases: Optional[Decimal] = Decimal("0")
        unconverted_buys = 0
        for t in trades:
            if not buy_sign(t):
                continue
            amount = await to_base.convert(
                t.proceeds if t.proceeds is not None else Decimal("0"), t.currency, t.trade_date
            )
            if amount is None:
                unconverted_buys += 1
                continue
            purchases -= amount
        if unconverted_buys:
            purchases = None
            warnings.append(
                f"{unconverted_buys} purchase(s) have no exchange rate for their trade "
                f"date, so the turnover is absent."
            )
        # The sales side is the realized section's total. A sale it had to omit leaves it
        # short (its warning says so); the closed-lot estimate is a whole answer, tagged.
        sales_side: Optional[Decimal] = (
            None if realized_source == "trades" and sales_unconvertible else sales_proceeds
        )
        criteria["purchases"] = None if purchases is None else round(float(purchases), 2)
        criteria["sales_proceeds"] = None if sales_side is None else round(float(sales_side), 2)
        criteria["turnover_estimated"] = (
            realized_source == "closed_lot_estimate" and has_realized_rows
        )
        volume = None if purchases is None or sales_side is None else purchases + sales_side

        # The opening base: its own snapshot call, with its latch read right after it,
        # and the IBKR cash that day through the one cash read.
        securities: Optional[Decimal] = None
        try:
            rows = await portfolio.holdings_snapshot_as_of(
                base_fx, opening_on, exclude_accounts=exempt_accounts
            )
        except Exception as e:
            logger.error(f"Holdings snapshot for {opening_on} failed: {e}", exc_info=True)
            warnings.append(
                f"The {opening_on.isoformat()} holdings snapshot could not be built, so the "
                f"turnover is absent."
            )
        else:
            skipped = portfolio.last_snapshot_skipped
            securities = sum((r["market_value"] for r in rows), Decimal("0"))
            criteria["opening_securities"] = round(float(securities), 2)
            if skipped:
                warnings.append(
                    f"The {opening_on.isoformat()} holdings behind the turnover omit "
                    f"{len(skipped)} security(ies) that could not be valued that day "
                    f"({', '.join(skipped)}), so the turnover overstates."
                )
        opening_cash = (await cash.uninvested_cash(base_fx, opening_on, account=IBKR)).balance
        if opening_cash is not None:
            criteria["opening_cash"] = round(float(opening_cash), 2)
        criteria["turnover_multiple"] = turnover_multiple(volume, securities, opening_cash)
        return criteria

    async def _lots_closed_by_sales(
        self, sales: Dict, exempt_accounts: List[str]
    ) -> List[TaxLot]:
        """The lots a SELL closed on one of `sales`' dates, of one of their securities."""
        security_ids = {
            t.security_id for rows in sales.values() for t in rows if t.security_id is not None
        }
        if not security_ids:
            return []
        return list((await self.db.execute(
            select(TaxLot)
            .join(Security, TaxLot.security_id == Security.id)
            .where(
                TaxLot.security_id.in_(security_ids),
                TaxLot.close_date.in_({sale_date for _, sale_date in sales}),
                TaxLot.close_source == "trade",
                *([Security.account.notin_(exempt_accounts)] if exempt_accounts else []),
            )
        )).scalars().all())

    def to_csv(self, report: Dict) -> str:
        cur = report["base_currency"]
        buf = io.StringIO()
        w = csv.writer(buf)

        w.writerow([f"Tax report {report['year']} — all amounts in {cur}"])
        w.writerow([])

        # The honesty flags are badged in the CSV as well as the UI, and a figure
        # that is missing rather than zero has to say so where it is read.
        if report.get("warnings"):
            w.writerow(["WARNINGS"])
            for warning in report["warnings"]:
                w.writerow([warning])
            w.writerow([])

        # Pillar 3a leads, because it explains what is *absent* from every section
        # below it. A reader reconciling the Steuerwert against a bank statement
        # needs that before the numbers, not in a footnote after them.
        p3a = report.get("pillar3a")
        if p3a:
            w.writerow(["Pillar 3a (excluded from every section below)"])
            w.writerow(["Contributions this year (deductible from taxable income)",
                        p3a["contributions"]])
            w.writerow(["Assets held (NOT part of the Steuerwert)",
                        "unavailable" if p3a["holdings_value"] is None
                        else p3a["holdings_value"]])
            w.writerow([p3a["note"]])
            w.writerow([])

        w.writerow([f"Dividend income (source: {report['dividend_source']})"])
        if report.get("dividend_source") == "mixed" and report.get("dividend_ibkr_from"):
            w.writerow([
                f"Estimates before {report['dividend_ibkr_from']}; "
                f"IBKR actuals with real withholding from there on"
            ])
        # An estimate's withholding is a blank cell, never 0.00: a reader filing DA-1
        # from this sheet must not read "nothing withheld" into "not known".
        w.writerow(["Symbol", "Description", "ISIN", "Pay date",
                    f"Gross ({cur})", f"Withholding ({cur})", f"Net ({cur})", "Estimate"])
        for d in report["dividend_income"]:
            estimated = d["source"] == YFINANCE_SOURCE
            w.writerow([d["symbol"], d["description"], d["isin"], d["pay_date"],
                        d["gross"], "" if d["withholding"] is None else d["withholding"],
                        d["net"], "est." if estimated else ""])
        dt = report["dividend_totals"]
        n_est = dt.get("estimated_rows", 0)
        w.writerow(["TOTAL", "", "", "", dt["gross"],
                    "" if dt["withholding"] is None else dt["withholding"],
                    dt["net"],
                    f"withholding excludes, gross and net include {n_est} est." if n_est else ""])
        # The current year: received plus expected, in the same columns.
        proj = report.get("projected")
        if proj:
            w.writerow([f"THROUGH {proj['through']}", "", "", "", proj["gross"],
                        _blank(proj["withholding"]), "", "est."])
        w.writerow([])

        # DA-1 is filed per source country, and credits only up to the treaty rate.
        w.writerow(["Withholding by country (DA-1)"])
        w.writerow(["Country", "Positions",
                    f"Gross ({cur})", f"Withholding ({cur})", "Rate (%)", "Treaty rate (%)",
                    f"Creditable ({cur})", f"Excess ({cur})", f"Net ({cur})",
                    "Estimated payments"])
        for c in report.get("dividend_by_country", []):
            w.writerow([c["country"], c["positions"], c["gross"],
                        _blank(c["withholding"]), _blank(c.get("rate_pct")),
                        _blank(c.get("residual_pct")), _blank(c.get("creditable")),
                        _blank(c.get("excess")), c["net"],
                        c.get("estimated_payments") or ""])
        w.writerow([])

        w.writerow([f"Realized capital gains (source: {report.get('realized_source', 'trades')})"])
        w.writerow(["Symbol", "ISIN", "Trade date", "Quantity",
                    f"Proceeds ({cur})", f"Cost basis ({cur})", f"Gain/Loss ({cur})"])
        for r in report["realized_gains"]:
            w.writerow([r["symbol"], _blank(r.get("isin")), r["trade_date"], r["quantity"],
                        r["proceeds"], r["cost_basis"], r["gain_loss"]])
        rt = report["realized_totals"]
        w.writerow(["TOTAL", "", "", "", rt["proceeds"], rt["cost_basis"], rt["gain_loss"]])
        w.writerow([])

        w.writerow([f"Holdings snapshot (wealth-tax base) as at {report.get('holdings_as_of', '')}"])
        if report.get("holdings_snapshot_error"):
            w.writerow(["Unavailable — the snapshot could not be built. This is not zero."])
        else:
            w.writerow(["Symbol", "ISIN", "Quantity",
                        f"Market value ({cur})", f"Cost basis ({cur})"])
            for h in report["holdings_snapshot"]:
                w.writerow([h["symbol"], _blank(h.get("isin")), h["quantity"],
                            h["market_value"], h["cost_basis"]])
            w.writerow(["TOTAL", "", "", report["holdings_snapshot_total"], ""])
        # Beside the holdings, not inside their total: a separate line on the return.
        if report.get("cash_balance") is not None:
            w.writerow(["Cash (IBKR)", "", "", report["cash_balance"], ""])

        tc = report.get("trader_criteria")
        if tc:
            def _yes_no(flag):
                return "" if flag is None else ("yes" if flag else "no")

            w.writerow([])
            w.writerow(["Securities-dealer indicators (ESTV KS 36)"])
            w.writerow(["Turnover (x)", _blank(tc["turnover_multiple"]),
                        "est." if tc["turnover_estimated"] else ""])
            w.writerow([f"Purchases ({cur})", _blank(tc["purchases"])])
            w.writerow([f"Sales proceeds ({cur})", _blank(tc["sales_proceeds"])])
            w.writerow([f"Opening securities at {tc['opening_as_of']} ({cur})",
                        _blank(tc["opening_securities"])])
            w.writerow([f"Opening cash (IBKR) at {tc['opening_as_of']} ({cur})",
                        _blank(tc["opening_cash"])])
            w.writerow(["Sales", _blank(tc["sales"])])
            w.writerow(["Sales held under 6 months", _blank(tc["short_held_sales"])])
            w.writerow(["Sales with unknown holding period", tc["sales_holding_unknown"]])
            w.writerow(["Margin used", _yes_no(tc["margin_used"])])
            w.writerow([f"Lowest cash balance ({cur})", _blank(tc["min_cash_balance"])])

        return buf.getvalue()
