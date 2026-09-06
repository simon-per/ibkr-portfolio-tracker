"""
Tax Service
Assembles a per-year tax report from authoritative IBKR data, framed for Swiss
filing:
  * Dividend income with foreign withholding tax (DA-1 reclaim) — real figures
    from <CashTransactions> when available, else yfinance-estimated gross.
  * Realized capital gains per SELL trade (Switzerland doesn't tax private
    capital gains, but the figures are provided for completeness / other regimes).
  * A current holdings snapshot as an indicative wealth-tax (Steuerwert) base.

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

from app.accounts import tax_exempt_accounts
from app.models.security import Security
from app.models.trade import Trade
from app.models.dividend_payment import DividendPayment
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.services.currency_service import CurrencyService
from app.services.dividend_service import DividendService
from app.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)


class TaxService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.currency_service = CurrencyService(db)

    async def _to_eur(
        self, amount: Decimal, currency: str, on_date: date
    ) -> Optional[Decimal]:
        """
        Convert to EUR, or return None when no rate can be resolved.

        It used to return the *unconverted* amount on failure, which reports a
        foreign figure as base currency — a TWD sale would read ~35x high in a
        filing aid still badged realized_source='trades', the badge that means
        authoritative. Every other FX consumer skips the row with a warning
        instead (sync_helper, portfolio_service, benchmark_service); this one
        silently produced a number, and logged nothing at all.
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
                f"No {currency}->EUR rate for {on_date} ({e}); "
                f"skipping the row rather than reporting {currency} as EUR"
            )
            return None

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
            div_gross += gross
            div_wht += wht
            div_net += net
            row_source = "ibkr" if dp.source == "ibkr" else "yfinance_estimate"
            sources_seen.add(row_source)
            dividend_income.append({
                "symbol": sec.symbol,
                "description": sec.description,
                "isin": sec.isin,
                "pay_date": on_date.isoformat(),
                "gross": round(float(gross), 2),
                "withholding": round(float(wht), 2),
                "net": round(float(net), 2),
                "source": row_source,
            })

            # First two ISIN characters are the issuer's domicile — a good proxy for the
            # withholding country, though not exact (ADRs and some funds differ).
            country = (sec.isin or "")[:2].upper()
            bucket = by_country.setdefault(country or "??", {
                "country": country or "??",
                "gross": Decimal("0"), "withholding": Decimal("0"), "net": Decimal("0"),
                "symbols": set(),
            })
            bucket["gross"] += gross
            bucket["withholding"] += wht
            bucket["net"] += net
            if sec.symbol:
                bucket["symbols"].add(sec.symbol)

        dividend_income.sort(key=lambda d: d["pay_date"])

        dividend_by_country = sorted(
            (
                {
                    "country": b["country"],
                    "gross": round(float(b["gross"]), 2),
                    "withholding": round(float(b["withholding"]), 2),
                    "net": round(float(b["net"]), 2),
                    "positions": len(b["symbols"]),
                }
                for b in by_country.values()
            ),
            key=lambda d: d["withholding"],
            reverse=True,
        )

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
        trades_skipped = 0
        skipped_currencies = set()
        for t, sec in trade_rows:
            if (t.buy_sell or "").upper() != "SELL":
                continue
            proceeds_e = await self._to_eur(t.proceeds or Decimal("0"), t.currency, t.trade_date)
            gain_e = await self._to_eur(t.realized_pnl or Decimal("0"), t.currency, t.trade_date)
            if proceeds_e is None or gain_e is None:
                # Reporting the raw foreign amount would be worse than omitting
                # it: the figure looks plausible and the badge says authoritative.
                trades_skipped += 1
                skipped_currencies.add(t.currency or "?")
                continue
            cost_e = proceeds_e - gain_e
            proceeds = base_fx.convert(proceeds_e, t.trade_date)
            gain = base_fx.convert(gain_e, t.trade_date)
            cost = base_fx.convert(cost_e, t.trade_date)
            r_proceeds += proceeds
            r_cost += cost
            r_gain += gain
            realized_gains.append({
                "symbol": t.symbol or (sec.symbol if sec else None),
                "trade_date": t.trade_date.isoformat(),
                "quantity": float(abs(t.quantity)) if t.quantity is not None else None,
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
                    "trade_date": row["close_date"].isoformat(),
                    "quantity": float(abs(row["quantity"])) if row["quantity"] is not None else None,
                    "proceeds": round(float(proceeds), 2),
                    "cost_basis": round(float(cost), 2),
                    "gain_loss": round(float(gain), 2),
                })

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
        today = date.today()
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
                converted = base_fx.convert(flow.amount_eur, flow.flow_date)
                if converted is not None:
                    exempt_contributions += converted
            # Stated so the reader can see the assets exist and are deliberately
            # absent from the Steuerwert, rather than wondering where they went.
            for row in await portfolio.holdings_snapshot_as_of(
                base_fx, as_of, only_accounts=exempt_accounts
            ):
                exempt_holdings_total += row["market_value"]
            warnings.append(
                "Pillar 3a is excluded from the wealth-tax base, the DA-1 reclaim "
                "and realized gains above, because 3a capital is not taxed as "
                "wealth and its income is not taxable income — it is taxed on "
                "withdrawal, at a separate reduced rate. Its contributions are "
                "reported separately below, being deductible."
            )

        # The label reflects what actually contributed inside the year window:
        # "mixed" is the boundary year, where estimates cover the weeks before the
        # cash-transaction ledger began mid-year.
        if sources_seen == {"ibkr"}:
            dividend_source = "ibkr"
        elif "ibkr" in sources_seen:
            dividend_source = "mixed"
        else:
            dividend_source = "yfinance_estimate"

        return {
            "year": year,
            "base_currency": base_fx.base_currency,
            "dividend_source": dividend_source,
            "dividend_ibkr_from": ibkr_from.isoformat() if ibkr_from else None,
            "dividend_income": dividend_income,
            "dividend_by_country": dividend_by_country,
            "dividend_country_note": (
                "Country derived from the ISIN prefix (issuer domicile) — a good proxy for the "
                "DA-1 source country, but verify ADRs and funds, whose withholding country can differ."
            ),
            "dividend_totals": {
                "gross": round(float(div_gross), 2),
                "withholding": round(float(div_wht), 2),
                "net": round(float(div_net), 2),
            },
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
                "holdings_value": round(float(exempt_holdings_total), 2),
                "note": (
                    "Pillar 3a contributions are deductible from taxable income. "
                    "The assets themselves are excluded from every section above: "
                    "3a capital is not part of the Steuerwert and its income is not "
                    "taxable, being taxed on withdrawal at a separate reduced rate. "
                    "Check the year's federal cap — it differs depending on whether "
                    "you are affiliated to a pension fund."
                ),
            } if has_exempt else None,
            "warnings": warnings,
        }

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
                        p3a["holdings_value"]])
            w.writerow([p3a["note"]])
            w.writerow([])

        w.writerow([f"Dividend income (source: {report['dividend_source']})"])
        if report.get("dividend_source") == "mixed" and report.get("dividend_ibkr_from"):
            w.writerow([
                f"Estimates before {report['dividend_ibkr_from']}; "
                f"IBKR actuals with real withholding from there on"
            ])
        w.writerow(["Symbol", "Description", "ISIN", "Pay date",
                    f"Gross ({cur})", f"Withholding ({cur})", f"Net ({cur})"])
        for d in report["dividend_income"]:
            w.writerow([d["symbol"], d["description"], d["isin"], d["pay_date"],
                        d["gross"], d["withholding"], d["net"]])
        dt = report["dividend_totals"]
        w.writerow(["TOTAL", "", "", "", dt["gross"], dt["withholding"], dt["net"]])
        w.writerow([])

        # DA-1 is filed per source country.
        w.writerow(["Withholding by country (DA-1)"])
        w.writerow(["Country", "Positions",
                    f"Gross ({cur})", f"Withholding ({cur})", f"Net ({cur})"])
        for c in report.get("dividend_by_country", []):
            w.writerow([c["country"], c["positions"], c["gross"], c["withholding"], c["net"]])
        w.writerow([])

        w.writerow([f"Realized capital gains (source: {report.get('realized_source', 'trades')})"])
        w.writerow(["Symbol", "Trade date", "Quantity",
                    f"Proceeds ({cur})", f"Cost basis ({cur})", f"Gain/Loss ({cur})"])
        for r in report["realized_gains"]:
            w.writerow([r["symbol"], r["trade_date"], r["quantity"],
                        r["proceeds"], r["cost_basis"], r["gain_loss"]])
        rt = report["realized_totals"]
        w.writerow(["TOTAL", "", "", rt["proceeds"], rt["cost_basis"], rt["gain_loss"]])
        w.writerow([])

        w.writerow([f"Holdings snapshot (wealth-tax base) as at {report.get('holdings_as_of', '')}"])
        if report.get("holdings_snapshot_error"):
            w.writerow(["Unavailable — the snapshot could not be built. This is not zero."])
        else:
            w.writerow(["Symbol", "Quantity", f"Market value ({cur})", f"Cost basis ({cur})"])
            for h in report["holdings_snapshot"]:
                w.writerow([h["symbol"], h["quantity"], h["market_value"], h["cost_basis"]])
            w.writerow(["TOTAL", "", report["holdings_snapshot_total"], ""])

        return buf.getvalue()
