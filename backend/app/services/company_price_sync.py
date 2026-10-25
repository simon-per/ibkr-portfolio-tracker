"""
The Company tab's share prices from Yahoo (docs/company-financials.md, *Prices*): one
history request per company, kept as one close a week, for the P/E in the key figures and
on the P/E card. Run by the weekly scheduled job (Sunday, after the week's last close)
under `SYNC_PIPELINE` like every Yahoo job, and by `app/cli/fetch_company_prices.py`.

**Scope** is the companies the tab lists (`CompanyFinancialsService.list_companies`):
held, and the current Nasdaq-100. By CIK with the SEC's ticker, which is Yahoo's spelling
for a US listing (BRK-B); no `securities` row is read, so a constituent nobody holds never
gets one and `yahoo_eligible` has nothing to filter.

**One basis.** Yahoo split-adjusts a history to the day it is fetched, so a history stored
across a split is two bases. A fetch reaches back `OVERLAP_DAYS` before the newest stored
close; a split Yahoo reports in that window that is not stored yet, an overlapping close
that moved by more than `BASIS_TOLERANCE`, or another currency, means the stored basis is
stale, and the whole history is fetched again and replaced as one set (refuse whole). The
service scales a close to the EPS basis with the splits after the company's newest filing.

**Kept:** the last trading day of each week, from `HISTORY_LEAD_DAYS` before the
company's first stored period; Yahoo's splits; `minor_unit_scale` applied.

**Bounded:** a company is asked at most once in `STALE_AFTER`; an empty answer backs off
on the shared doubling schedule (`prices_backoff_until`); a 429, or the process-wide hold,
abandons the pass (`yahoo_rate_limit`).
"""

import asyncio
import logging
import random
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yfinance as yf
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models.company_financials import SecCompany
from app.repositories.company_financials_repository import (
    CompanyFinancialsRepository,
    prices_backoff_until,
)
from app.services.edgar_client import sec_ticker
from app.services.yahoo_minor_units import (
    minor_unit,
    minor_unit_scale,
    reported_currency_of,
)
from app.services.yahoo_rate_limit import check_yahoo_hold, is_rate_limit

logger = logging.getLogger(__name__)

SCHEDULED_SYNC_TYPE = "company_prices_refresh"
MANUAL_SYNC_TYPE = "manual_company_prices_fetch"

STALE_AFTER = timedelta(days=6)
OVERLAP_DAYS = 21
HISTORY_LEAD_DAYS = 400
BASIS_TOLERANCE = 0.02
# Between two requests, as the other Yahoo loops pace themselves.
REQUEST_DELAY_SECONDS = (2.0, 5.0)

RATE_LIMITED_WARNING = (
    "Company prices: Yahoo rate-limited the pass; the remaining companies keep their "
    "previous closes until next week."
)

Closes = List[Tuple[date, float]]


def weekly_closes(hist, scale) -> Tuple[Dict[date, float], Closes, Closes]:
    """
    A daily `history()` frame as (every close by date, the last close of each week, the
    splits), each amount divided by the minor-unit `scale`. NaN closes are skipped.
    """
    daily: Dict[date, float] = {}
    splits: Closes = []
    for index, row in hist.iterrows():
        day = index.date()
        close = row.get("Close")
        if close is not None and close == close and close > 0:
            daily[day] = float(close) / float(scale)
        ratio = row.get("Stock Splits")
        if ratio is not None and ratio == ratio and ratio > 0:
            splits.append((day, float(ratio)))
    weeks: Dict[tuple, Tuple[date, float]] = {}
    for day in sorted(daily):
        weeks[day.isocalendar()[:2]] = (day, daily[day])
    return daily, sorted(weeks.values()), splits


def yahoo_currency(ticker: str, reported: Optional[str]) -> Optional[str]:
    """The major currency code of a Yahoo report (`GBp` is GBP once scaled)."""
    minor = minor_unit(reported)
    if minor:
        return minor[0]
    return reported.upper() if reported else None


class CompanyPriceSync:
    def __init__(self, db: AsyncSession, sleep=asyncio.sleep):
        self.db = db
        self.repo = CompanyFinancialsRepository(db)
        self._sleep = sleep

    async def _targets(self, symbols: Optional[Sequence[str]]) -> List[SecCompany]:
        """The companies the tab lists, narrowed to `symbols` (either listing's or the SEC's)."""
        from app.services.company_financials_service import CompanyFinancialsService

        listed = await CompanyFinancialsService(self.db).list_companies()
        companies = await self.repo.companies()
        wanted = {sec_ticker(s) for s in symbols} if symbols else None
        out = []
        for entry in listed:
            company = companies.get(entry["cik"])
            if company is None or not company.ticker:
                continue
            if wanted is not None and not (
                {company.ticker, sec_ticker(entry["symbol"])} & wanted
            ):
                continue
            out.append(company)
        return out

    def _due(self, company: SecCompany, now: datetime) -> bool:
        if company.prices_fetched_at is not None and (
            company.prices_fetched_at > now - STALE_AFTER
        ):
            return False
        until = prices_backoff_until(company)
        return until is None or until <= now

    async def _history(self, ticker: str, start: Optional[date]):
        """One Yahoo request: daily closes from `start`, or the whole history."""
        await check_yahoo_hold()
        await self._sleep(random.uniform(*REQUEST_DELAY_SECONDS))

        def _fetch():
            t = yf.Ticker(ticker)
            if start is None:
                hist = t.history(period="max", auto_adjust=False)
            else:
                hist = t.history(start=start, auto_adjust=False)
            return hist, reported_currency_of(t)

        return await asyncio.to_thread(_fetch)

    async def refresh(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        symbols: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """
        One pass; each company is committed as it is written. `force` asks again
        regardless of staleness and back-off; `dry_run` lists what is due and asks nothing.
        """
        now = utcnow()
        targets = await self._targets(symbols)
        due = [c for c in targets if force or self._due(c, now)]
        summary: Dict[str, Any] = {
            "status": "success",
            "in_scope": len(targets),
            "companies_due": len(due),
            "fetched": 0,
            "refetched": 0,
            "rows_written": 0,
            "no_data": 0,
            "errors": 0,
            "rate_limited": False,
            "warnings": [],
        }
        if dry_run:
            summary["due"] = [c.ticker for c in due]
            summary["message"] = (
                f"Company prices: {len(due)} of {len(targets)} due (dry run)"
            )
            return summary

        # Plain values: a rollback below expires the ORM rows.
        pairs = [(c.cik, c.ticker) for c in due]
        coverage = await self.repo.coverage(cik for cik, _ in pairs)
        for cik, ticker in pairs:
            first = (coverage.get(cik) or {}).get("first")
            try:
                await self._one(cik, ticker, first, summary)
            except Exception as e:
                await self.db.rollback()
                if is_rate_limit(e):
                    summary["rate_limited"] = True
                    summary["status"] = "rate_limited"
                    summary["warnings"].append(RATE_LIMITED_WARNING)
                    logger.warning(f"Company prices: rate limited at {ticker}: {e}")
                    break
                summary["errors"] += 1
                logger.warning(f"Company prices for {ticker} failed: {e}")
        if summary["errors"]:
            summary["warnings"].append(
                f"Company prices: {summary['errors']} compan"
                f"{'y' if summary['errors'] == 1 else 'ies'} could not be fetched and keep "
                f"their previous closes."
            )
        summary["message"] = (
            f"Company prices: {summary['fetched']} of {len(due)} due fetched, "
            f"{summary['rows_written']} weekly closes written"
        )
        return summary

    async def _one(
        self, cik: int, ticker: str, first_period: Optional[date], summary
    ) -> None:
        stored = await self.repo.prices_for(cik)
        stored_splits = {s.date for s in await self.repo.splits_for(cik)}
        since = stored[-1].date - timedelta(days=OVERLAP_DAYS) if stored else None

        hist, reported = await self._history(ticker, since)
        if hist.empty and since is not None:
            # Nothing new in the window (a long halt): keep what is stored.
            (await self.repo.ensure_company(cik)).prices_fetched_at = utcnow()
            await self.db.commit()
            summary["fetched"] += 1
            return
        currency = yahoo_currency(ticker, reported)
        if hist.empty or currency is None:
            await self.repo.record_price_miss(cik, utcnow())
            await self.db.commit()
            summary["no_data"] += 1
            return
        daily, closes, splits = weekly_closes(hist, minor_unit_scale(ticker, reported))

        if since is not None:
            by_date = {p.date: p for p in stored if p.date >= since}
            moved = (
                any(d not in stored_splits for d, _ in splits)
                or any(p.currency != currency for p in by_date.values())
                or any(
                    d in daily and abs(daily[d] / p.close - 1) > BASIS_TOLERANCE
                    for d, p in by_date.items()
                )
            )
            if moved:
                logger.info(
                    f"Company prices: {ticker}'s basis moved; fetching it whole"
                )
                hist, reported = await self._history(ticker, None)
                currency = yahoo_currency(ticker, reported)
                if hist.empty or currency is None:
                    await self.repo.record_price_miss(cik, utcnow())
                    await self.db.commit()
                    summary["no_data"] += 1
                    return
                daily, closes, splits = weekly_closes(
                    hist, minor_unit_scale(ticker, reported)
                )
                since = None
                summary["refetched"] += 1

        if since is None and first_period is not None:
            start = first_period - timedelta(days=HISTORY_LEAD_DAYS)
            closes = [(d, c) for d, c in closes if d >= start]
        await self.repo.write_prices(
            cik, closes, splits, currency, utcnow(), since=since
        )
        await self.db.commit()
        summary["fetched"] += 1
        summary["rows_written"] += len(closes)
