"""
Where the return came from — the analytics layer above the per-security attribution.

Three questions, each answered from tables that already exist and with **no upstream
request** (CLAUDE.md rule 1: nothing here can reach Yahoo or IBKR):

1. **Return decomposition.** The change in Total Value over a window, split into
   what was paid in (external flows), what the holdings earned in their own
   currency (price), what the base currency's moves added or took away (FX), the
   dividend cash that landed, the broker's interest/fees/spread, and whatever is
   left. The parts sum to the change *exactly*, by construction: the last leg is
   the remainder, named `unexplained_eur` and never hidden.

2. **Segment attribution.** The same per-security gains folded through the
   look-through onto sectors and countries, so "which part of the book made the
   money" can be answered at the level the funds hide. An approximation and badged
   as one: a fund's gain is spread by its *current* basket weights, so it assumes the
   mix inside the fund was constant over the window.

3. **Closed positions.** What the sold positions realized, how long they were held,
   and what they did after they were sold — the one figure that judges the decision
   rather than the holding.

Everything per-security comes from `PortfolioService.attribution_rows`, which is the
same arithmetic behind `/api/portfolio/attribution`. That is deliberate and load-
bearing: the stacked bar here and the per-security bar chart there must agree about
every gain to the cent, and a second loop over the same lots is how such figures stop
agreeing (CLAUDE.md, *two implementations*).

Conventions that hold throughout, each a bug elsewhere first:
- An unknown is `None`, never `0` — a zero FX effect *claims* the currency did not move.
- An unvaluable holding is excluded from both sides and counted (`unpriced_holdings`).
- Nothing is renormalised; shortfalls are named (`unexplained_eur`, `unsplit_eur`,
  the *Fund residual* and *Unknown* segments).
"""
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.etf_mappings import is_known_etf_isin, symbol_for_fund_isin
from app.etf_sources import basket_proxy_for
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.repositories.market_price_repository import MarketPriceRepository
from app.services.cash_service import CashService, UNKNOWN as CASH_UNKNOWN
from app.services.dividend_service import DividendService
from app.services.lookthrough_service import LookthroughService
from app.services.native_amounts import NativeToBase
from app.services.portfolio_service import PortfolioService
from app.services.sector_taxonomy import UNKNOWN as SECTOR_UNKNOWN, normalise as normalise_sector

logger = logging.getLogger(__name__)

HUNDRED = Decimal("100")
ZERO = Decimal("0")

#: Segment names for value the look-through cannot place. Kept as constants so the
#: frontend can recognise them, and phrased as what they ARE rather than as a fault.
FUND_RESIDUAL = "Fund residual (cash, derivatives, nested funds)"
FUND_UNCOVERED = "Funds without a basket"
UNKNOWN_COUNTRY = "Unknown"


def _f(value: Optional[Decimal], places: int = 2) -> Optional[float]:
    """Decimal → rounded float, preserving None (an unknown stays absent)."""
    if value is None:
        return None
    return round(float(value), places)


def _pct(part: Decimal, whole: Decimal) -> Optional[float]:
    if whole == 0:
        return None
    return round(float(part / whole * HUNDRED), 2)


class PerformanceAnalyticsService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.portfolio = PortfolioService(db)

    # ------------------------------------------------------------------ decomposition

    async def decomposition(self, start_date: date, end_date: date) -> Dict:
        """
        The window's return split into its sources, plus the same split per calendar
        year the account has existed. See the module docstring for the legs.

        Identity, on the *rounded* figures the response carries::

            end_total_value − start_total_value
              = net_flows + price + fx + unsplit + dividends + fees_interest + unexplained

        `unexplained_eur` is what the ledgers cannot account for: in-kind transfers
        (a lot arrives with a cost and no cash left), the gap between a sale's real
        proceeds and the market-price approximation the lot carries, trading
        commissions, and — when cash is not tracked at all — every sale's proceeds,
        because they leave the total value with nowhere to land. It is reported, and
        a warning names the likeliest cause, rather than being folded into a leg that
        would then claim it.
        """
        base_fx = await self.portfolio._load_base_fx()
        inputs = await self.portfolio._contribution_inputs(base_fx)
        first_open: Optional[date] = inputs["first_open"]

        # One timeline over the widest span any window below needs; every window reads
        # its two endpoint values off it, so the years and the window cannot disagree
        # about what the account was worth on a day.
        span_start = start_date
        if first_open is not None:
            span_start = min(span_start, date(first_open.year, 1, 1) - timedelta(days=1))
        timeline = await self.portfolio.get_portfolio_value_over_time(span_start, end_date)
        by_date = {date.fromisoformat(p["date"]): p for p in timeline}
        cash_source = await CashService(self.db).cash_source()

        dividends = [
            (when, base_fx.convert(net_eur, when))
            for when, net_eur in await DividendService(self.db).ibkr_cash_receipts()
        ]
        corrections = await CashService(self.db).measured_corrections(base_fx)
        flows: List[Tuple[date, Decimal]] = inputs["money_in_legs"]

        async def window(ws: date, we: date) -> Dict:
            return await self._decompose_window(
                ws, we, by_date, dividends, corrections, flows, cash_source,
            )

        result = await window(start_date, end_date)

        years: List[Dict] = []
        if first_open is not None:
            for year in range(first_open.year, end_date.year + 1):
                y_start = date(year, 1, 1) - timedelta(days=1)   # (31 Dec, 31 Dec]
                y_end = min(date(year, 12, 31), end_date)
                if y_end <= y_start:
                    continue
                row = await window(y_start, y_end)
                row["year"] = year
                row["partial"] = y_end < date(year, 12, 31) or (
                    first_open > date(year, 1, 1)
                )
                years.append(row)

        return {
            "base_currency": base_fx.base_currency,
            "cash_source": cash_source,
            "window": result,
            "years": years,
        }

    async def _decompose_window(
        self,
        start_date: date,
        end_date: date,
        by_date: Dict[date, Dict],
        dividends: List[Tuple[date, Decimal]],
        corrections: List[Tuple[date, Decimal, str]],
        flows: List[Tuple[date, Decimal]],
        cash_source: str,
    ) -> Dict:
        computed = await self.portfolio.attribution_rows(start_date, end_date)
        eff_start = computed["effective_start"]
        eff_end = computed["effective_end"]
        warnings: List[str] = []

        start_point = self._point_on_or_before(by_date, eff_start)
        end_point = self._point_on_or_before(by_date, eff_end)

        empty = {
            "start_date": eff_start.isoformat(),
            "end_date": eff_end.isoformat(),
            "start_total_value_eur": None,
            "end_total_value_eur": None,
            "net_flows_eur": None,
            "gain_eur": None,
            "gain_pct": None,
            "price_effect_eur": None,
            "fx_effect_eur": None,
            "unsplit_eur": None,
            "unsplit_securities": 0,
            "dividends_eur": None,
            "fees_interest_eur": None,
            "unexplained_eur": None,
            "unpriced_holdings": 0,
            "warnings": warnings,
        }
        if computed["empty"] or start_point is None or end_point is None:
            warnings.append(
                "Nothing could be valued in this window — no priced holdings on either end."
            )
            return empty

        priced = {
            sid: r for sid, r in computed["rows"].items() if sid not in computed["unpriced"]
        }
        price_effect = ZERO
        fx_effect = ZERO
        unsplit = ZERO
        unsplit_count = 0
        for r in priced.values():
            if r["price_effect_base"] is None:
                unsplit += r["pnl_base"]
                unsplit_count += 1
            else:
                price_effect += r["price_effect_base"]
                fx_effect += r["fx_effect_base"]

        in_window = lambda d: eff_start < d <= eff_end  # noqa: E731 — (start, end]
        dividends_eur = sum((a for d, a in dividends if in_window(d)), ZERO)
        fees_eur = sum((a for d, a, _ in corrections if in_window(d)), ZERO)
        net_flows = sum((a for d, a in flows if in_window(d)), ZERO)

        start_tv = Decimal(str(start_point["total_value_eur"]))
        end_tv = Decimal(str(end_point["total_value_eur"]))
        # A pre-inception start point reads 0 / 0 / 0 and is a real, measurable zero
        # (docs/frontend.md, *A pre-inception day is measurable*), so the first year's
        # gain is measured from nothing held — which is what happened.
        gain = end_tv - start_tv - net_flows
        holdings_gain = price_effect + fx_effect + unsplit

        # Round the legs, then take the remainder on the rounded figures so the
        # response's parts sum exactly (the `fund_residual_eur` rule).
        legs = {
            "net_flows_eur": _f(net_flows),
            "price_effect_eur": _f(price_effect),
            "fx_effect_eur": _f(fx_effect),
            "unsplit_eur": _f(unsplit),
            "dividends_eur": _f(dividends_eur),
            "fees_interest_eur": _f(fees_eur),
        }
        start_f = _f(start_tv)
        end_f = _f(end_tv)
        unexplained = round(end_f - start_f - sum(legs.values()), 2)

        unpriced = len(computed["unpriced"])
        # The timeline's own count at either endpoint can exceed the attribution's
        # (it also walks securities the window never priced); report the larger, since
        # either means the total is incomplete.
        unpriced = max(
            unpriced, int(start_point.get("unpriced_holdings") or 0),
            int(end_point.get("unpriced_holdings") or 0),
        )
        if unpriced:
            warnings.append(
                f"{unpriced} holding{'s' if unpriced != 1 else ''} could not be priced at "
                f"an endpoint and {'are' if unpriced != 1 else 'is'} left out of every leg, "
                f"so the figures cover less than the whole book."
            )
        if unsplit_count:
            warnings.append(
                f"{unsplit_count} holding{'s' if unsplit_count != 1 else ''} could not be "
                f"split into price and FX (no rate for its currency on a needed date); "
                f"its gain is carried whole as 'unsplit'."
            )
        if cash_source == CASH_UNKNOWN:
            warnings.append(
                "Cash is not tracked for this account, so sale proceeds leave the total "
                "value with nowhere to land — expect a negative 'unexplained' of about the "
                "window's disposals."
            )
        elif abs(unexplained) > 0.5 and abs(unexplained) > 0.001 * max(float(end_tv), 1.0):
            warnings.append(
                "The unexplained remainder is in-kind transfers, trading commissions and "
                "the gap between a sale's real proceeds and the market price its lot "
                "carries. It is reported rather than folded into another leg."
            )

        # Modified Dietz over the window: flows assumed mid-period. None when the
        # denominator is not positive — a window starting from nothing has no base to
        # measure a percentage against.
        denominator = start_tv + net_flows / 2
        gain_pct = _pct(gain, denominator) if denominator > 0 else None

        return {
            "start_date": eff_start.isoformat(),
            "end_date": eff_end.isoformat(),
            "start_total_value_eur": start_f,
            "end_total_value_eur": end_f,
            "gain_eur": _f(gain),
            "gain_pct": gain_pct,
            **legs,
            "unsplit_securities": unsplit_count,
            "unexplained_eur": unexplained,
            "unpriced_holdings": unpriced,
            "warnings": warnings,
        }

    @staticmethod
    def _point_on_or_before(
        by_date: Dict[date, Dict], target: date, lookback_days: int = 7
    ) -> Optional[Dict]:
        for back in range(lookback_days + 1):
            p = by_date.get(target - timedelta(days=back))
            if p is not None:
                return p
        return None

    # -------------------------------------------------------------------- segments

    async def segments(self, start_date: date, end_date: date) -> Dict:
        """
        Per-security gains folded onto sectors and countries through the look-through.

        A direct holding lands on its own `securities.sector` / `.country` (written by
        the allocation sync; NULL until someone runs it, which reads as *Unknown*
        rather than as a fault). A fund's gain, start value and end value are spread by
        its stored basket's company rows — the current basket, so the split assumes the
        fund's mix was constant over the window and says so. Weight the basket cannot
        place (cash, derivatives, nested funds, rows without a weight) lands on
        `FUND_RESIDUAL`; a fund with no basket at all lands whole on `FUND_UNCOVERED`.
        Nothing is renormalised.
        """
        computed = await self.portfolio.attribution_rows(start_date, end_date)
        eff_start = computed["effective_start"]
        eff_end = computed["effective_end"]
        empty = {
            "start_date": eff_start.isoformat(),
            "end_date": eff_end.isoformat(),
            "total_pnl_eur": None,
            "start_total_value_eur": None,
            "end_total_value_eur": None,
            "unpriced_holdings": 0,
            "basket_as_of_oldest": None,
            "proxied_funds": [],
            "by_sector": [],
            "by_country": [],
            "warnings": [],
        }
        if computed["empty"]:
            return empty

        priced = {
            sid: r for sid, r in computed["rows"].items() if sid not in computed["unpriced"]
        }
        securities = computed["securities"]

        # Baskets for the held funds, with proxies aliased the way the look-through does.
        fund_isins = sorted({
            securities[sid].isin.strip().upper()
            for sid in priced if is_known_etf_isin(securities[sid].isin)
        })
        proxy_sources = sorted({
            src for i in fund_isins if (src := basket_proxy_for(i)) and src != i
        })
        repo = EtfBasketRepository(self.db)
        baskets = await repo.get_baskets(fund_isins + proxy_sources)
        holdings = await repo.get_holdings(
            [i for i in fund_isins + proxy_sources if i in baskets]
        )
        proxied_from = LookthroughService._alias_proxied_baskets(fund_isins, baskets, holdings)

        by_sector: Dict[str, Dict] = {}
        by_country: Dict[str, Dict] = {}
        warnings: List[str] = []
        basket_dates: List[date] = []

        def bucket(store: Dict[str, Dict], name: str) -> Dict:
            return store.setdefault(name, {
                "name": name,
                "pnl": ZERO, "price": ZERO, "fx": ZERO, "unsplit": ZERO,
                "start": ZERO, "end": ZERO, "via_funds": ZERO,
            })

        def add(store, name, r, share: Decimal, via_fund: bool):
            b = bucket(store, name)
            b["pnl"] += r["pnl_base"] * share
            if r["price_effect_base"] is None:
                b["unsplit"] += r["pnl_base"] * share
            else:
                b["price"] += r["price_effect_base"] * share
                b["fx"] += r["fx_effect_base"] * share
            b["start"] += r["start_mv_base"] * share
            b["end"] += r["end_mv_base"] * share
            if via_fund:
                b["via_funds"] += r["end_mv_base"] * share

        for sid, r in priced.items():
            sec = securities[sid]
            isin = (sec.isin or "").strip().upper()
            if not is_known_etf_isin(isin):
                add(by_sector, normalise_sector(sec.sector), r, Decimal("1"), False)
                add(by_country, sec.country or UNKNOWN_COUNTRY, r, Decimal("1"), False)
                continue

            basket = baskets.get(isin)
            if basket is None:
                add(by_sector, FUND_UNCOVERED, r, Decimal("1"), True)
                add(by_country, FUND_UNCOVERED, r, Decimal("1"), True)
                continue
            basket_dates.append(basket.as_of_date)

            placed_sector = ZERO
            placed_country = ZERO
            for row in holdings.get(isin, []):
                if not LookthroughService._counts_as_company(row, basket):
                    continue
                share = Decimal(row.weight_pct) / HUNDRED
                if share <= 0:
                    continue
                add(by_sector, normalise_sector(row.sector), r, share, True)
                add(by_country, row.country or UNKNOWN_COUNTRY, r, share, True)
                placed_sector += share
                placed_country += share
            # Whatever the basket could not place stays visible as its own segment.
            if placed_sector < 1:
                add(by_sector, FUND_RESIDUAL, r, Decimal("1") - placed_sector, True)
            if placed_country < 1:
                add(by_country, FUND_RESIDUAL, r, Decimal("1") - placed_country, True)

        total_pnl = sum((r["pnl_base"] for r in priced.values()), ZERO)
        start_total = sum((r["start_mv_base"] for r in priced.values()), ZERO)
        end_total = sum((r["end_mv_base"] for r in priced.values()), ZERO)

        def present(store: Dict[str, Dict]) -> List[Dict]:
            out = []
            for b in store.values():
                has_split = b["unsplit"] == 0
                out.append({
                    "name": b["name"],
                    "pnl_eur": _f(b["pnl"]),
                    "price_effect_eur": _f(b["price"]) if has_split else None,
                    "fx_effect_eur": _f(b["fx"]) if has_split else None,
                    "share_of_gain_pct": _pct(b["pnl"], total_pnl),
                    "start_value_eur": _f(b["start"]),
                    "end_value_eur": _f(b["end"]),
                    "start_weight_pct": _pct(b["start"], start_total),
                    "end_weight_pct": _pct(b["end"], end_total),
                    "via_funds_pct": _pct(b["via_funds"], b["end"]) if b["end"] else None,
                })
            out.sort(key=lambda s: -abs(s["pnl_eur"] or 0))
            return out

        if proxied_from:
            names = ", ".join(
                f"{symbol_for_fund_isin(i) or i} (via {symbol_for_fund_isin(s) or s})"
                for i, s in sorted(proxied_from.items())
            )
            warnings.append(
                f"Decomposed through a borrowed basket: {names}. Those segments describe "
                f"the proxy's index, not the fund's own."
            )
        if basket_dates:
            warnings.append(
                "Fund gains are spread by each fund's current basket, so the split assumes "
                "the mix inside every fund was constant over the window. Oldest basket: "
                f"{min(basket_dates).isoformat()}."
            )
        if computed["unpriced"]:
            n = len(computed["unpriced"])
            warnings.append(
                f"{n} holding{'s' if n != 1 else ''} could not be priced at an endpoint and "
                f"{'are' if n != 1 else 'is'} left out of every segment."
            )
        if SECTOR_UNKNOWN in by_sector or UNKNOWN_COUNTRY in by_country:
            warnings.append(
                "'Unknown' carries directly held securities the allocation sync has not "
                "classified, and basket rows their issuer publishes without a sector or "
                "country."
            )

        return {
            "start_date": eff_start.isoformat(),
            "end_date": eff_end.isoformat(),
            "total_pnl_eur": _f(total_pnl),
            "start_total_value_eur": _f(start_total),
            "end_total_value_eur": _f(end_total),
            "unpriced_holdings": len(computed["unpriced"]),
            "basket_as_of_oldest": min(basket_dates).isoformat() if basket_dates else None,
            "proxied_funds": sorted(proxied_from),
            "by_sector": present(by_sector),
            "by_country": present(by_country),
            "warnings": warnings,
        }

    # ------------------------------------------------------------- closed positions

    async def closed_positions(self) -> Dict:
        """
        Every security with at least one closed lot: what it realized, how long it was
        held, and what its price did after the last sale.

        Realized P&L is IBKR's own FIFO figure summed over the security's SELL trades
        when any exist (`source: trade`), otherwise the market-price approximation over
        its closed lots that `realized_rows_from_closed_lots` shares with the tax report
        (`source: closed_lots`). Cost is the closed lots' cost basis in both cases, so
        the return percentage is against what was actually paid.

        `post_sale_pct` needs a price *after* the last close, which a fully sold
        security usually stops receiving — the market-data sync only prices open
        positions. It is None then, with `post_sale_days` saying how far the record
        reaches, rather than a 0 that would claim the price stood still.
        """
        base_fx = await self.portfolio._load_base_fx()
        rows = await self.portfolio.realized_rows_from_closed_lots(base_fx)
        if not rows:
            return self._closed_empty(base_fx.base_currency)

        open_lot_sids = {
            sid for (sid,) in (await self.db.execute(
                select(TaxLot.security_id).where(TaxLot.is_open == True).distinct()  # noqa: E712
            )).all()
        }
        securities: Dict[int, Security] = {
            s.id: s for s in (await self.db.execute(
                select(Security).where(Security.id.in_({r["security_id"] for r in rows}))
            )).scalars().all()
        }

        # IBKR's own realized figures per security, where SELL trades exist.
        to_base = NativeToBase(self.portfolio.currency_service, base_fx)
        trade_pnl: Dict[int, Decimal] = {}
        trade_fx_missing: set = set()
        for t in (await self.db.execute(
            select(Trade).where(Trade.security_id.isnot(None))
        )).scalars().all():
            if (t.buy_sell or "").upper() != "SELL":
                continue
            gain = await to_base.convert(
                t.realized_pnl if t.realized_pnl is not None else ZERO,
                t.currency or "EUR", t.trade_date,
            )
            if gain is None:
                trade_fx_missing.add(t.security_id)
                continue
            trade_pnl[t.security_id] = trade_pnl.get(t.security_id, ZERO) + gain

        grouped: Dict[int, Dict] = {}
        for r in rows:
            g = grouped.setdefault(r["security_id"], {
                "cost": ZERO, "proceeds": ZERO, "lots": 0,
                "first_open": r["open_date"], "last_close": r["close_date"],
                "weighted_days": ZERO, "quantity": ZERO,
            })
            g["cost"] += r["cost_basis"]
            g["proceeds"] += r["proceeds"]
            g["lots"] += 1
            g["quantity"] += r["quantity"]
            g["first_open"] = min(g["first_open"], r["open_date"])
            g["last_close"] = max(g["last_close"], r["close_date"])
            g["weighted_days"] += r["cost_basis"] * (r["close_date"] - r["open_date"]).days

        price_repo = MarketPriceRepository(self.db)
        positions: List[Dict] = []
        for sid, g in grouped.items():
            sec = securities.get(sid)
            if sec is None:
                continue
            if sid in trade_pnl:
                realized = trade_pnl[sid]
                source = "trade"
            else:
                realized = g["proceeds"] - g["cost"]
                source = "closed_lots"
            holding_days = (
                int(g["weighted_days"] / g["cost"]) if g["cost"] > 0 else None
            )
            return_pct = _pct(realized, g["cost"]) if g["cost"] > 0 else None

            # Compared in the QUOTE currency, from the price history itself: the last
            # close on or before the sale date against the newest close after it. The
            # base-currency proceeds cannot serve as the reference — they carry FX —
            # and a quote in a different currency (a repaired mapping) is refused
            # rather than compared.
            post_sale_pct: Optional[float] = None
            post_sale_days: Optional[int] = None
            latest = await price_repo.get_latest_price(sid)
            if latest is not None and latest.date > g["last_close"]:
                around_close = await price_repo.get_price_range(
                    sid, g["last_close"] - timedelta(days=14), g["last_close"]
                )
                ref = around_close[-1] if around_close else None
                if (
                    ref is not None and ref.close_price and ref.close_price > 0
                    and ref.currency == latest.currency
                ):
                    post_sale_pct = _pct(latest.close_price - ref.close_price, ref.close_price)
                    post_sale_days = (latest.date - g["last_close"]).days

            positions.append({
                "security_id": sid,
                "symbol": sec.symbol,
                "description": sec.description or sec.symbol,
                "account": sec.account,
                "still_held": sid in open_lot_sids,
                "lots_closed": g["lots"],
                "first_open_date": g["first_open"].isoformat(),
                "last_close_date": g["last_close"].isoformat(),
                "holding_days": holding_days,
                "cost_basis_eur": _f(g["cost"]),
                "proceeds_eur": _f(g["proceeds"]),
                "realized_pnl_eur": _f(realized),
                "realized_source": source,
                "return_pct": return_pct,
                "post_sale_pct": post_sale_pct,
                "post_sale_days": post_sale_days,
            })

        positions.sort(key=lambda p: -abs(p["realized_pnl_eur"] or 0))

        judged = [p for p in positions if p["realized_pnl_eur"] is not None]
        winners = [p for p in judged if p["realized_pnl_eur"] > 0]
        losers = [p for p in judged if p["realized_pnl_eur"] < 0]
        total_cost = sum((g["cost"] for g in grouped.values()), ZERO)
        weighted_days = sum((g["weighted_days"] for g in grouped.values()), ZERO)
        post_judged = [p for p in positions if p["post_sale_pct"] is not None]

        warnings: List[str] = []
        if trade_fx_missing:
            warnings.append(
                f"{len(trade_fx_missing)} security's SELL trades had no FX rate on the "
                f"trade date and fell back to the closed-lot approximation."
            )
        if positions and not post_judged:
            warnings.append(
                "No sold position has a price after its last sale on record — the "
                "market-data sync prices open positions only — so post-sale performance "
                "is unmeasurable for all of them."
            )

        return {
            "base_currency": base_fx.base_currency,
            "positions": positions,
            "summary": {
                "closed_securities": len(positions),
                "winners": len(winners),
                "losers": len(losers),
                "hit_rate_pct": _pct(Decimal(len(winners)), Decimal(len(judged))) if judged else None,
                "total_realized_eur": _f(sum((Decimal(str(p["realized_pnl_eur"])) for p in judged), ZERO)) if judged else None,
                "total_cost_eur": _f(total_cost),
                "avg_holding_days": int(weighted_days / total_cost) if total_cost > 0 else None,
                "best": winners[0]["symbol"] if winners else None,
                "worst": min(losers, key=lambda p: p["realized_pnl_eur"])["symbol"] if losers else None,
                "post_sale_judged": len(post_judged),
                "sold_then_rose": sum(1 for p in post_judged if p["post_sale_pct"] > 0),
                "sold_then_fell": sum(1 for p in post_judged if p["post_sale_pct"] < 0),
            },
            "warnings": warnings,
        }

    @staticmethod
    def _closed_empty(base_currency: str) -> Dict:
        return {
            "base_currency": base_currency,
            "positions": [],
            "summary": {
                "closed_securities": 0, "winners": 0, "losers": 0,
                "hit_rate_pct": None, "total_realized_eur": None, "total_cost_eur": None,
                "avg_holding_days": None, "best": None, "worst": None,
                "post_sale_judged": 0, "sold_then_rose": 0, "sold_then_fell": 0,
            },
            "warnings": [],
        }
