"""
Yahoo's company-level figures for the Company tab's companies nobody holds — a chunk a run.

The Company tab's overview (docs/company-financials.md, *The overview*) shows the analyst
rating and targets, forward P/E, PEG, forward growth, the next earnings date and what the
company does. For a held company those come from its listing's own rows, which the daily
held pass already keeps (`FundamentalsService`, `AnalystRatingService`). For the ~84
Nasdaq-100 companies nobody holds they come from here: **CLAUDE.md rule 1, enabled by the
owner on 2026-11-07 for exactly this budget — in chunks, never a burst.**

- **The chunk**: each company once per `ROTATION_DAYS`, oldest first, so a run asks
  `chunk_size(n)` companies — about 9 at `REQUESTS_PER_COMPANY` each, ~60 requests. A first
  fill takes a rotation; that is intended. Never a held company (the held pass covers it).
- **Inside the 05:00 `fundamentals_refresh`, after the held pass and the ratings**: the same
  `SYNC_PIPELINE` gate, the same process-wide hold (`check_yahoo_hold`, awaited by both
  fetchers), skipped when either pass was rate-limited, and no route of its own.
- **The same readers**: `FundamentalsService._fetch_yahoo_data` and `extract_metrics` /
  `extract_earnings`, and `AnalystRatingService.fetch_counts` — no second `.info` reader.
- **Refuse whole**: a company's figures and rating counts are written as one set, or not at
  all. Yahoo with nothing, or a failure that is not a rate limit, backs the company off on the
  shared schedule (`miss_backoff`); a rate limit abandons the chunk and stamps nothing.
"""

import asyncio
import logging
import math
import random
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.repositories.company_financials_repository import (
    CompanyFinancialsRepository,
    yahoo_profile_backoff_until,
)
from app.services.analyst_rating_service import AnalystRatingService
from app.services.company_financials_service import CompanyFinancialsService
from app.services.fundamentals_service import (
    FundamentalsService,
    extract_earnings,
    extract_metrics,
)
from app.services.yahoo_rate_limit import check_yahoo_hold, is_rate_limit

logger = logging.getLogger(__name__)

#: Each company the tab lists and nobody holds is refreshed once in this many days.
ROTATION_DAYS = 14
#: The 05:00 `fundamentals_refresh` runs Tuesday to Saturday
#: (`scheduler_service.FUNDAMENTALS_REFRESH_DAYS`; a test pins the two together).
RUNS_PER_WEEK = 5
RUNS_PER_ROTATION = ROTATION_DAYS * RUNS_PER_WEEK // 7
#: A company written during a run is due at the same slot a rotation later, not the next.
ROTATION_SLACK = timedelta(hours=4)
#: `_fetch_yahoo_data`'s six (`.info` is three, earnings dates, two estimate frames) and
#: `fetch_counts`' one — what one company costs.
REQUESTS_PER_COMPANY = 7


def chunk_size(companies: int) -> int:
    """How many companies one run asks about: the rotation spread over its runs."""
    return math.ceil(companies / RUNS_PER_ROTATION) if companies > 0 else 0


class CompanyProfileSync:
    """One run's chunk. Built per run, so `rate_limited` never leaks forward."""

    rate_limited = False

    def __init__(self, db: AsyncSession, sleep: Callable[[float], Any] = asyncio.sleep):
        self.db = db
        self.repo = CompanyFinancialsRepository(db)
        self.rate_limited = False
        self._sleep = sleep

    async def index_only(self) -> List[Dict[str, Any]]:
        """
        The companies the Company tab lists that nobody holds — its own list
        (`list_companies`), so the chunk asks about exactly what the tab can show. A company
        without an SEC ticker on record has nothing to ask Yahoo by.
        """
        companies = await CompanyFinancialsService(self.db).list_companies()
        return [
            c for c in companies if not c["held"] and not str(c["symbol"]).isdigit()
        ]

    async def refresh(self) -> Dict[str, Any]:
        companies = await self.index_only()
        rows = await self.repo.yahoo_profiles(c["cik"] for c in companies)
        now = utcnow()

        def waiting(cik: int) -> bool:
            until = yahoo_profile_backoff_until(rows.get(cik))
            return until is not None and now < until

        def last_written(cik: int) -> datetime:
            row = rows.get(cik)
            return (
                row.fetched_at if row is not None and row.fetched_at else datetime.min
            )

        fresh_after = now - timedelta(days=ROTATION_DAYS) + ROTATION_SLACK
        backing = [c for c in companies if waiting(c["cik"])]
        due = sorted(
            (
                c
                for c in companies
                if not waiting(c["cik"]) and last_written(c["cik"]) <= fresh_after
            ),
            key=lambda c: (last_written(c["cik"]), c["symbol"]),
        )
        chunk = due[: chunk_size(len(companies))]
        summary: Dict[str, Any] = {
            "status": "success",
            "in_scope": len(companies),
            "due": len(due),
            "asked": 0,
            "refreshed": 0,
            "no_data": 0,
            "errors": 0,
            "backing_off": len(backing),
            "rate_limited": False,
        }

        fundamentals = FundamentalsService(self.db)
        ratings = AnalystRatingService(self.db)
        for i, company in enumerate(chunk):
            cik, ticker = company["cik"], company["symbol"]
            try:
                # Before the pacing too: a hold set by the held pass is honoured at once.
                await check_yahoo_hold()
                if i:
                    await self._sleep(random.uniform(2.0, 4.0))
                summary["asked"] += 1
                (
                    info,
                    earnings_df,
                    quarterly,
                    eps_trend,
                    revenue_estimate,
                ) = await fundamentals._fetch_yahoo_data(ticker)
                metrics = extract_metrics(
                    info, quarterly, eps_trend, revenue_estimate, ticker
                )
                if metrics is None:
                    await self._record_miss(cik)
                    summary["no_data"] += 1
                    continue
                counts = await ratings.fetch_counts(ticker, ticker)
                if counts is None:
                    # Refuse whole: the figures without the rating that goes with them
                    # would be half of one answer beside half of the last one.
                    await self._record_miss(cik)
                    summary["errors"] += 1
                    continue
                upcoming = [
                    e for e in extract_earnings(earnings_df, ticker) if e["is_upcoming"]
                ]
                nearest = min(upcoming, key=lambda e: e["earnings_date"], default=None)
                await self.repo.write_yahoo_profile(
                    cik,
                    {
                        **metrics,
                        **counts,
                        "next_earnings_date": nearest["earnings_date"]
                        if nearest
                        else None,
                        "next_eps_estimate": nearest["eps_estimate"]
                        if nearest
                        else None,
                    },
                    utcnow(),
                )
                await self.db.commit()
                summary["refreshed"] += 1
            except Exception as e:
                await self.db.rollback()
                if is_rate_limit(e):
                    self.rate_limited = True
                    summary["rate_limited"] = True
                    logger.warning(
                        f"Yahoo rate limit at {ticker} ({i + 1}/{len(chunk)}); "
                        f"abandoning the rest of the company chunk"
                    )
                    break
                logger.error(f"Company figures for {ticker} failed: {e}")
                summary["errors"] += 1
                await self._record_miss(cik)

        summary["message"] = (
            f"Company figures: {summary['refreshed']} of {summary['asked']} refreshed "
            f"({summary['due']} due of {summary['in_scope']} not held)"
        )
        warnings = []
        if self.rate_limited:
            warnings.append(
                "Yahoo Finance rate limit reached; the Company tab's chunk was abandoned. "
                "Do not retry manually — the next scheduled run resumes where it stopped."
            )
        if summary["errors"]:
            n = summary["errors"]
            warnings.append(
                f"{n} compan{'y' if n == 1 else 'ies'} could not refresh Yahoo's figures "
                f"and keep their previous ones."
            )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _record_miss(self, cik: int) -> None:
        """Stamp a miss as the only write. Its own failure costs the stamp, never the run."""
        try:
            await self.repo.record_yahoo_miss(cik, utcnow())
            await self.db.commit()
        except Exception as e:
            await self.db.rollback()
            logger.error(f"Could not stamp the Yahoo miss of CIK {cik}: {e}")
