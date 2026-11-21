"""
Closes for another regulator's company without a Yahoo request (docs/company-financials.md,
*Prices without Yahoo*).

A Korean or European company gets no Yahoo request (owner, 2026-11-17), so its multiples had no
price. Two sources the app already pays for carry one:

- **The held listing's daily closes** (`market_prices`), fetched for the portfolio anyway, of
  the listing whose ISIN is the company's own (Samsung's common share, never its preferred,
  which trades at a discount). The price sync purges and refetches a listing's history when
  IBKR books a split (`PRICE_RESTATING_ACTIONS`), so the history is on today's basis.
- **The iShares basket files** the evening refresh already downloads: every constituent row
  carries `unitPrice` and `unitsHeld` (`parse_ishares`), and `record_constituent_prices` keeps
  each day's price for the companies of another regulator (`constituent_prices`).

**The basket's price is in the fund's base currency, not the listing's.** Measured on IWDA's
file of 2026-11-18: units x price equals the row's market value for every row, Apple's 352.45
sat beside its 356.27 USD weekly close, and Toyota's 21.29 is dollars, not yen. The payload
names no base currency (its `currencyCode` is the share class's GBP), so each fund declares it
(`FundSource.params["price_currency"]`) and every recording checks the declaration against
the SEC companies' Yahoo closes in the same basket (`VERIFY_*`): a wrong declaration refuses
the day's prices instead of scaling every European multiple by an exchange rate.

**A split in the basket** shows as the fund's units multiplying while the price divides by the
same ratio (`basket_splits`); the closes before it are put on the new basis, as Yahoo adjusts a
history, and `eps_basis` then restates them to the figures' basis.
"""

import logging
from datetime import date, timedelta
from fractions import Fraction
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_financials import (
    OTHER_SCHEMES,
    Company,
    CompanyPrice,
    SecCompany,
)
from app.models.etf_basket import ConstituentPrice
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services.company_financials_normaliser import SPLIT_RATIOS
from app.services.fx_preload import eur_rate_on, preload_eur_rates

logger = logging.getLogger(__name__)

#: The declared base currency of a fund's basket prices (`app/etf_sources.py`).
PRICE_CURRENCY_PARAM = "price_currency"
#: The declaration holds when the median basket price over the closes the app already holds
#: for the same ISINs (within `VERIFY_MATCH_DAYS` before the basket's date: an SEC company's
#: newest weekly close, else a held listing's newest daily one) is this near 1, over at least
#: `VERIFY_MIN_MATCHES` companies. A week's moves stay within a few percent in the median;
#: the nearest wrong currency (EUR for USD) is off by over 10%.
VERIFY_TOLERANCE = 0.06
VERIFY_MIN_MATCHES = 5
VERIFY_MATCH_DAYS = 7
#: A split between two of one fund's rows: the units moved by a split ratio within
#: `UNITS_SPLIT_TOLERANCE` and the price by its inverse within `PRICE_SPLIT_TOLERANCE`.
UNITS_SPLIT_TOLERANCE = 0.02
PRICE_SPLIT_TOLERANCE = 0.10
#: One close a week, the newest of each ISO week, as the SEC filers' weekly closes.
Close = Tuple[date, float]


async def record_constituent_prices(
    db: AsyncSession, basket, price_currency: Optional[str]
) -> Dict[str, Any]:
    """
    Keep the basket's price of every company of another regulator, and of every SEC filer
    priced by its ordinary share (a 20-F filer found by its name, `ordinary_priced_ciks`),
    once per ISIN and day (`constituent_prices`), when the fund declares its price currency
    and the declaration holds against the closes the app holds (`_verify`). Nothing is written
    otherwise; the outcome says why.
    """
    out: Dict[str, Any] = {"recorded": 0, "verified": None}
    if not price_currency:
        out["skipped"] = "no declared price currency"
        return out
    wanted = {
        (isin or "").strip().upper()
        for isin in (
            await db.execute(
                select(Company.isin).where(
                    Company.scheme.in_(OTHER_SCHEMES), Company.isin.is_not(None)
                )
            )
        ).scalars()
    }
    ordinary = await CompanyFinancialsRepository(db).ordinary_priced_ciks()
    if ordinary:
        wanted |= {
            (isin or "").strip().upper()
            for isin in (
                await db.execute(
                    select(SecCompany.isin).where(SecCompany.cik.in_(sorted(ordinary)))
                )
            ).scalars()
        }
    priced = [
        r
        for r in basket.rows
        if r.isin in wanted and r.price is not None and r.price > 0
    ]
    if not priced:
        return out
    verdict = await _verify(db, basket, price_currency)
    out["verified"] = verdict["holds"]
    out.update({k: v for k, v in verdict.items() if k != "holds"})
    if not verdict["holds"]:
        return out
    have = set(
        (
            await db.execute(
                select(ConstituentPrice.isin).where(
                    ConstituentPrice.date == basket.as_of_date,
                    ConstituentPrice.isin.in_([r.isin for r in priced]),
                )
            )
        ).scalars()
    )
    for row in priced:
        if row.isin in have:
            continue
        have.add(row.isin)
        db.add(
            ConstituentPrice(
                isin=row.isin,
                date=basket.as_of_date,
                price=float(row.price),
                currency=price_currency,
                fund_isin=basket.fund_isin,
                units_held=float(row.units_held)
                if row.units_held is not None
                else None,
            )
        )
        out["recorded"] += 1
    return out


async def _verify(db: AsyncSession, basket, price_currency: str) -> Dict[str, Any]:
    """
    Whether the basket's prices are in `price_currency`: the median ratio to the closes the
    app already holds for the same ISINs, each converted at the basket's date — an SEC
    company's newest weekly close (`company_prices`), else a held listing's newest daily one
    (`market_prices`). An emerging-markets basket holds almost no SEC filer with a Yahoo close
    (IEEM two on 2026-11-21) but the held listings of Samsung, SK hynix, NU and TSMC's
    ordinary share: without them its prices were refused, and Taiwan's, Tencent's and China
    Construction Bank's pages had no multiple.
    """
    by_isin = {
        r.isin: r for r in basket.rows if r.isin and r.price is not None and r.price > 0
    }
    since = basket.as_of_date - timedelta(days=VERIFY_MATCH_DAYS)
    references: Dict[str, Tuple[float, str]] = {}
    ciks = dict(
        (
            await db.execute(
                select(SecCompany.isin, SecCompany.cik).where(
                    SecCompany.isin.in_(list(by_isin))
                )
            )
        ).all()
    )
    if ciks:
        closes: Dict[int, CompanyPrice] = {}
        for p in (
            await db.execute(
                select(CompanyPrice)
                .where(
                    CompanyPrice.cik.in_(list(ciks.values())),
                    CompanyPrice.date >= since,
                    CompanyPrice.date <= basket.as_of_date,
                )
                .order_by(CompanyPrice.date)
            )
        ).scalars():
            closes[p.cik] = p
        for isin, cik in ciks.items():
            close = closes.get(cik)
            if close is not None and close.close > 0:
                references[isin] = (float(close.close), close.currency)
    held = [isin for isin in by_isin if isin not in references]
    if held:
        for isin, close, currency in (
            await db.execute(
                select(Security.isin, MarketPrice.close_price, MarketPrice.currency)
                .join(MarketPrice, MarketPrice.security_id == Security.id)
                .where(
                    Security.isin.in_(held),
                    MarketPrice.date >= since,
                    MarketPrice.date <= basket.as_of_date,
                )
                .order_by(MarketPrice.date)
            )
        ).all():
            if close is not None and close > 0:
                references[isin] = (float(close), currency)
    if not references:
        return {"holds": False, "matches": 0, "reason": "no close to compare"}
    rates = await preload_eur_rates(
        db,
        {price_currency, *(currency for _, currency in references.values())},
        basket.as_of_date,
        basket.as_of_date,
    )
    ratios = []
    for isin, (close, currency) in references.items():
        rate = cross_rate(rates, price_currency, currency, basket.as_of_date)
        if rate is None:
            continue
        ratios.append(float(by_isin[isin].price) * rate / close)
    if len(ratios) < VERIFY_MIN_MATCHES:
        return {
            "holds": False,
            "matches": len(ratios),
            "reason": "too few closes to compare",
        }
    middle = median(ratios)
    if abs(middle - 1) > VERIFY_TOLERANCE:
        logger.warning(
            f"{basket.fund_isin}: basket prices are not in {price_currency} "
            f"(median ratio {middle:.3f} over {len(ratios)} closes); none kept"
        )
        return {
            "holds": False,
            "matches": len(ratios),
            "median_ratio": round(middle, 4),
            "reason": f"prices are not in {price_currency}",
        }
    return {"holds": True, "matches": len(ratios), "median_ratio": round(middle, 4)}


def cross_rate(
    rates, from_currency: str, to_currency: str, day: date
) -> Optional[float]:
    """`from -> to` on `day` over the preloaded EUR rates (`fx_preload`); None when either
    leg is missing within the carry bound."""
    if from_currency == to_currency:
        return 1.0
    legs = []
    for currency in (from_currency, to_currency):
        if currency == "EUR":
            legs.append(1.0)
            continue
        rate = eur_rate_on(rates, currency, day)
        if rate is None:
            return None
        legs.append(float(rate))
    return legs[0] / legs[1]


def basket_splits(rows: Sequence[ConstituentPrice]) -> List[Tuple[date, float]]:
    """
    The splits one ISIN's stored rows show, as (date, new shares per old): between two
    consecutive rows of one fund, units held multiplied by a split ratio and the price
    divided by it.
    """
    out: List[Tuple[date, float]] = []
    last: Dict[str, ConstituentPrice] = {}
    candidates = [r for r in SPLIT_RATIOS if r != Fraction(1)]
    for row in sorted(rows, key=lambda r: r.date):
        before = last.get(row.fund_isin)
        last[row.fund_isin] = row
        if (
            before is None
            or not before.units_held
            or not row.units_held
            or before.price <= 0
            or row.price <= 0
        ):
            continue
        moved = row.units_held / before.units_held
        ratio = next(
            (
                float(c)
                for c in candidates
                if abs(moved / float(c) - 1) <= UNITS_SPLIT_TOLERANCE
            ),
            None,
        )
        if ratio is None:
            continue
        if abs((before.price / row.price) / ratio - 1) <= PRICE_SPLIT_TOLERANCE:
            out.append((row.date, ratio))
    return out


def weekly(closes: Iterable[Close]) -> List[Close]:
    """The newest close of each ISO week, oldest first."""
    by_week: Dict[Tuple[int, int], Close] = {}
    for day, close in sorted(closes):
        by_week[day.isocalendar()[:2]] = (day, close)
    return [by_week[w] for w in sorted(by_week)]


async def basket_closes(
    db: AsyncSession, isin: str, currency: str
) -> Tuple[List[Close], List[Tuple[date, float]]]:
    """
    One ISIN's basket closes in `currency` (converted at each day's ECB rate; a day without a
    rate is left out), every one on today's split basis, and the splits found
    (`basket_splits`) for `eps_basis`.
    """
    rows = list(
        (
            await db.execute(
                select(ConstituentPrice)
                .where(ConstituentPrice.isin == isin)
                .order_by(ConstituentPrice.date)
            )
        ).scalars()
    )
    if not rows:
        return [], []
    splits = basket_splits(rows)
    rates = await preload_eur_rates(
        db, {currency, *(r.currency for r in rows)}, rows[0].date, rows[-1].date
    )
    out: List[Close] = []
    for row in rows:
        rate = cross_rate(rates, row.currency, currency, row.date)
        if rate is None:
            continue
        scale = 1.0
        for day, ratio in splits:
            if row.date < day:
                scale /= ratio
        out.append((row.date, row.price * rate * scale))
    return weekly(out), splits


async def listing_closes(
    db: AsyncSession, security_id: int, currency: str
) -> List[Close]:
    """A held listing's daily closes (`market_prices`) in `currency`, weekly."""
    from app.repositories.market_price_repository import MarketPriceRepository

    rows = await MarketPriceRepository(db).get_price_range(
        security_id, date(1990, 1, 1), date.max
    )
    if not rows:
        return []
    rates = await preload_eur_rates(
        db, {currency, *(r.currency for r in rows)}, rows[0].date, rows[-1].date
    )
    out: List[Close] = []
    for row in rows:
        rate = cross_rate(rates, row.currency, currency, row.date)
        if rate is not None and row.close_price and row.close_price > 0:
            out.append((row.date, float(row.close_price) * rate))
    return weekly(out)
