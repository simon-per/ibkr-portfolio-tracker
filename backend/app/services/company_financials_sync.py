"""
The EDGAR pass: SEC filers' companyfacts in, `company_financials` rows out
(docs/company-financials.md). Run by the weekly scheduled job and by
`app/cli/fetch_company_financials.py`; both hold `EDGAR_GATE` and record a `sync_runs` row.

**Scope**, two parts, one request per company (CIK) whichever brought it in:

- **Held** (`in_scope`): held securities (`security_repository.held`) listed on a US
  exchange — the exchanges the price path maps to a bare ticker — that are not a known fund.
  Their ticker, spelled the SEC's way, finds the CIK; a non-US listing, or a ticker the SEC
  does not list, never reaches companyfacts.
- **The Nasdaq-100** (`universe`): the `UNIVERSE_SIZE` heaviest distinct companies of the
  stored basket of each fund in `UNIVERSE_FUNDS` — QQQM's, which the evening basket refresh
  keeps current, so a reconstitution reaches this list without a code change. Equity rows
  only (whitelisted); a constituent the account does not hold gets a `sec_companies` row and
  never a `securities` one. Each ticker's CIK, or the SEC's "no such ticker", is kept in
  `sec_tickers`. Each full pass stamps the companies it found
  (`universe_seen_at`), which is how the tab lists the current index and not a former member.

A foreign filer (ASML's and NU's 20-F) has a CIK but files IFRS, so its document yields no
row: a miss, backed off like any other.

**Bounded** (`STALE_AFTER`, `CIK_RECHECK_AFTER`, `miss_backoff`): a company is fetched at
most once in `STALE_AFTER`; a held ticker the SEC does not list is looked up again after
`CIK_RECHECK_AFTER`; the ticker list is asked at most once a pass; a CIK whose document has
nothing this app reads (a 404, a 20-F) is asked again on the shared doubling schedule. A
failed request (transport, 429, 5xx after the client's retries) stamps nothing and is asked
next pass; a 401/403 stops the pass.

**Without `EDGAR_USER_AGENT`** the pass makes no request and says so in its warnings.
Touches neither Yahoo nor IBKR.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.etf_mappings import is_known_etf_isin
from app.models.etf_basket import EtfHolding
from app.models.security import Security
from app.repositories.company_financials_repository import (
    CompanyFinancialsRepository,
    facts_backoff_until,
)
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.repositories.security_repository import held
from app.services.company_financials_normaliser import normalise
from app.services.edgar_client import (
    EdgarClient,
    EdgarError,
    EdgarRefused,
    is_configured,
    sec_ticker,
)
from app.services.etf_basket_parsers import INVESTED_ASSET_CLASSES
from app.services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)

#: The gate both entry points hold. Not `SYNC_PIPELINE`: EDGAR shares no upstream with
#: Yahoo or IBKR, and entering that gate would put the stock Sync buttons on cooldown.
EDGAR_GATE = "edgar"
SCHEDULED_SYNC_TYPE = "company_financials_refresh"
MANUAL_SYNC_TYPE = "manual_company_financials_fetch"

#: Under a week, so the weekly pass refetches every company it fetched the week before.
STALE_AFTER = timedelta(days=6)
CIK_RECHECK_AFTER = timedelta(days=30)

#: The funds whose stored baskets name the companies stored beyond the held ones: the
#: Nasdaq-100, through QQQM (owner's request, 2026-10-25). Symbols or ISINs.
UNIVERSE_FUNDS = ("QQQM",)
#: Companies, not lines: GOOG and GOOGL are one, so the index's 101 lines are 100.
UNIVERSE_SIZE = 100

NOT_CONFIGURED_WARNING = (
    "EDGAR_USER_AGENT is not set; company financials were not refreshed."
)


def us_exchanges() -> frozenset:
    """The exchanges whose listings the price path quotes under the bare ticker."""
    return frozenset(
        exchange
        for exchange, suffix in MarketDataService.EXCHANGE_SUFFIXES.items()
        if suffix == ""
    )


@dataclass
class Constituent:
    """One basket line the universe may take, heaviest first."""

    ticker: str  # the SEC's spelling
    name: str
    isin: Optional[str]


@dataclass
class Target:
    """One company due a look, and what brought it in (for the run's record)."""

    cik: int
    symbols: List[str] = field(default_factory=list)


class CompanyFinancialsSync:
    def __init__(
        self,
        db: AsyncSession,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.db = db
        self.repo = CompanyFinancialsRepository(db)
        self._transport = transport
        self._sleep = sleep

    async def in_scope(self, symbols: Optional[Sequence[str]] = None) -> List[Security]:
        """Held, US-listed, not a known fund; optionally only these symbols."""
        result = await self.db.execute(
            select(Security)
            .where(held(), Security.exchange.in_(sorted(us_exchanges())))
            .order_by(Security.symbol, Security.id)
        )
        wanted = {s.upper() for s in symbols} if symbols else None
        return [
            s
            for s in result.scalars()
            if not is_known_etf_isin(s.isin)
            and (wanted is None or s.symbol.upper() in wanted)
        ]

    async def universe(self) -> tuple:
        """
        The universe funds' equity constituents, heaviest first, one line per ticker;
        and the funds that have no stored basket.
        """
        baskets = EtfBasketRepository(self.db)
        lines: List[EtfHolding] = []
        missing: List[str] = []
        for ref in UNIVERSE_FUNDS:
            fund_isins = await baskets.fund_isins(ref)
            rows = (
                (
                    await self.db.execute(
                        select(EtfHolding)
                        .where(EtfHolding.fund_isin.in_(fund_isins))
                        .order_by(EtfHolding.weight_pct.desc(), EtfHolding.line_no)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                missing.append(ref)
            lines.extend(rows)
        out: Dict[str, Constituent] = {}
        for row in sorted(lines, key=lambda r: -r.weight_pct):
            # Whitelisted: a money-market fund, a future or cash is no company.
            if (row.asset_class or "").strip().lower() not in INVESTED_ASSET_CLASSES:
                continue
            if not row.constituent_ticker or is_known_etf_isin(row.constituent_isin):
                continue
            ticker = sec_ticker(row.constituent_ticker)
            out.setdefault(
                ticker, Constituent(ticker, row.constituent_name, row.constituent_isin)
            )
        return list(out.values()), missing

    async def refresh(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        symbols: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """
        One pass. Returns a summary for a `sync_runs` row; each company is committed as
        it is written, so a crash mid-pass keeps what was already fetched. `force` asks
        again regardless of staleness and back-off; `dry_run` lists what is due and makes
        no request and no write. `symbols` narrows both parts of the scope, and a narrowed
        pass leaves the universe's stamps alone.
        """
        configured = is_configured()
        if not configured and not dry_run:
            return {
                "status": "skipped",
                "message": "Company financials: skipped, EDGAR_USER_AGENT is not set",
                "warnings": [NOT_CONFIGURED_WARNING],
            }

        now = utcnow()
        wanted = {sec_ticker(s) for s in symbols} if symbols else None
        securities = await self.in_scope(symbols)
        constituents, missing_baskets = await self.universe()
        if wanted is not None:
            constituents = [c for c in constituents if c.ticker in wanted]
        stamps = await self.repo.cik_rows(s.id for s in securities)
        need_cik = [
            s
            for s in securities
            if s.id not in stamps
            or (
                stamps[s.id].cik is None
                and (force or stamps[s.id].cik_checked_at <= now - CIK_RECHECK_AFTER)
            )
        ]
        known, asked = await self._known_tickers()
        # A ticker asked before and not listed waits `CIK_RECHECK_AFTER`, like a held one.
        unknown = [
            c
            for c in constituents
            if c.ticker not in known
            and (
                force
                or c.ticker not in asked
                or asked[c.ticker] <= now - CIK_RECHECK_AFTER
            )
        ]
        summary: Dict[str, Any] = {
            "status": "success",
            "configured": configured,
            "in_scope": len(securities),
            "universe": 0,
            "cik_lookups": len(need_cik) + len(unknown),
            "companies_due": 0,
            "fetched": 0,
            "rows_written": 0,
            "no_data": 0,
            "errors": 0,
            "stopped": None,
            "outcomes": [],
        }
        warnings: List[str] = []
        if missing_baskets:
            warnings.append(
                f"No stored basket for {', '.join(missing_baskets)}; the Nasdaq-100 list "
                f"keeps the companies of the last pass that had one."
            )

        if dry_run:
            targets = self._targets(securities, stamps, constituents, known)
            due = await self._due(targets, now, force)
            summary["universe"] = sum(1 for c in constituents if c.ticker in known)
            summary["companies_due"] = len(due)
            summary["outcomes"] = [
                {"cik": t.cik, "symbols": t.symbols, "outcome": "due"} for t in due
            ]
            summary["message"] = (
                f"Company financials: {len(due)} companies due, "
                f"{summary['cik_lookups']} CIK lookups (dry run, no request made)"
            )
            if not configured:
                warnings.append(NOT_CONFIGURED_WARNING)
            if warnings:
                summary["warnings"] = warnings
            return summary

        async with EdgarClient(
            settings.edgar_user_agent.strip(),
            transport=self._transport,
            sleep=self._sleep,
        ) as client:
            try:
                if need_cik or unknown:
                    try:
                        tickers = await client.company_tickers()
                        for security in need_cik:
                            ticker = sec_ticker(security.symbol)
                            await self.repo.record_cik(
                                security.id, tickers.get(ticker), ticker, now
                            )
                        for constituent in unknown:
                            cik = tickers.get(constituent.ticker)
                            await self.repo.record_ticker(constituent.ticker, cik, now)
                            if cik is not None:
                                known[constituent.ticker] = cik
                        await self.db.commit()
                    except EdgarError as e:
                        summary["errors"] += 1
                        warnings.append(
                            f"The SEC ticker list could not be read ({e}); "
                            f"{summary['cik_lookups']} ticker(s) without a CIK wait for "
                            f"the next pass."
                        )
                    stamps = await self.repo.cik_rows(s.id for s in securities)
                targets = self._targets(securities, stamps, constituents, known)
                universe = await self._record_universe(
                    constituents, known, now, stamp=wanted is None
                )
                summary["universe"] = len(universe)
                await self.db.commit()
                due = await self._due(targets, now, force)
                summary["companies_due"] = len(due)
                for target in due:
                    outcome = await self._one(client, target.cik, summary)
                    summary["outcomes"].append(
                        {
                            "cik": target.cik,
                            "symbols": target.symbols,
                            "outcome": outcome,
                        }
                    )
            except EdgarRefused as refused:
                summary["stopped"] = refused.status
                hint = "" if refused.status == 429 else " Check EDGAR_USER_AGENT."
                warnings.append(
                    f"The SEC answered HTTP {refused.status}; the company financials "
                    f"pass stopped and the rest waits for the next one.{hint}"
                )

        failed = summary["errors"]
        n = sum(1 for o in summary["outcomes"] if o["outcome"].startswith("error"))
        if n:
            warnings.append(
                f"{n} compan{'y' if n == 1 else 'ies'} could not be fetched from EDGAR "
                f"and keep their previous figures."
            )
        summary["message"] = (
            f"Company financials: {summary['fetched']} of {summary['companies_due']} "
            f"companies fetched, {summary['rows_written']} rows, "
            f"{summary['no_data']} without 10-Q/10-K figures, {failed} failed"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _known_tickers(self) -> tuple:
        """
        SEC ticker -> CIK from what is already stored (no request needed for these), and
        ticker -> when the SEC last answered that it lists no such ticker.
        """
        known: Dict[str, int] = {}
        asked: Dict[str, datetime] = {}
        for row in (await self.repo.ticker_rows()).values():
            if row.cik is not None:
                known[row.ticker] = row.cik
            else:
                asked[row.ticker] = row.checked_at
        for row in await self.repo.all_cik_rows():
            if row.cik is not None and row.sec_ticker:
                known.setdefault(row.sec_ticker, row.cik)
        for company in (await self.repo.companies()).values():
            if company.ticker:
                known.setdefault(company.ticker, company.cik)
        return known, asked

    def _targets(
        self,
        securities: Sequence[Security],
        stamps,
        constituents: Sequence[Constituent],
        known: Dict[str, int],
    ) -> List[Target]:
        """Every company in scope, held ones first, then the universe heaviest first."""
        by_cik: Dict[int, Target] = {}
        for security in securities:
            row = stamps.get(security.id)
            if row is not None and row.cik is not None:
                by_cik.setdefault(row.cik, Target(row.cik)).symbols.append(
                    security.symbol
                )
        for cik in self._universe_ciks(constituents, known):
            target = by_cik.setdefault(cik, Target(cik))
            if not target.symbols:
                target.symbols.append(
                    next(c.ticker for c in constituents if known.get(c.ticker) == cik)
                )
        return list(by_cik.values())

    @staticmethod
    def _universe_ciks(
        constituents: Sequence[Constituent], known: Dict[str, int]
    ) -> List[int]:
        """The first `UNIVERSE_SIZE` distinct companies among the constituents."""
        ciks: List[int] = []
        for constituent in constituents:
            cik = known.get(constituent.ticker)
            if cik is not None and cik not in ciks:
                ciks.append(cik)
            if len(ciks) == UNIVERSE_SIZE:
                break
        return ciks

    async def _record_universe(
        self,
        constituents: Sequence[Constituent],
        known: Dict[str, int],
        now: datetime,
        stamp: bool,
    ) -> List[int]:
        ciks = self._universe_ciks(constituents, known)
        first = {}
        for constituent in constituents:
            cik = known.get(constituent.ticker)
            if cik is not None:
                first.setdefault(cik, constituent)
        for cik in ciks:
            c = first[cik]
            company = await self.repo.ensure_company(cik, c.ticker, c.name, c.isin)
            if stamp:
                company.universe_seen_at = now
        return ciks

    async def _due(
        self, targets: Sequence[Target], now: datetime, force: bool
    ) -> List[Target]:
        """The targets due a fetch: not backed off, not fetched within `STALE_AFTER`."""
        if force:
            return list(targets)
        companies = await self.repo.companies(t.cik for t in targets)
        due: List[Target] = []
        for target in targets:
            company = companies.get(target.cik)
            wait = facts_backoff_until(company)
            if wait is not None and wait > now:
                continue
            if (
                company is not None
                and company.fetched_at is not None
                and company.fetched_at > now - STALE_AFTER
            ):
                continue
            due.append(target)
        return due

    async def _one(self, client: EdgarClient, cik: int, summary: Dict[str, Any]) -> str:
        try:
            facts = await client.company_facts(cik)
        except EdgarError as e:
            summary["errors"] += 1
            logger.warning(f"companyfacts for CIK {cik} failed: {e}")
            return f"error: {e}"
        result = normalise(facts) if facts is not None else None
        if result is None or not result.has_data:
            await self.repo.record_miss(cik, utcnow())
            await self.db.commit()
            summary["no_data"] += 1
            return "no 10-Q/10-K figures" if facts is not None else "not found"
        await self.repo.replace_rows(cik, result.rows, utcnow(), result.entity_name)
        await self.db.commit()
        summary["fetched"] += 1
        summary["rows_written"] += len(result.rows)
        return f"{len(result.rows)} rows"
