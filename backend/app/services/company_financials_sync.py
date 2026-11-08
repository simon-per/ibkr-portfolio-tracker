"""
The EDGAR pass: SEC filers' companyfacts in, `company_financials` rows out
(docs/company-financials.md). Run by the weekly scheduled job and by
`app/cli/fetch_company_financials.py`; both hold `EDGAR_GATE` and record a `sync_runs` row.
**The weekday check** (`check_filings`) fetches, between the weekly passes, the companies
whose next 10-Q or 10-K the SEC now lists — one small request for each company whose filing
is due, so a report reaches the tab the next morning rather than on Sunday.

**Scope**, three parts, one request per company (CIK) whichever brought it in:

- **Held** (`in_scope`): held securities (`security_repository.held`) listed on a US
  exchange — the exchanges the price path maps to a bare ticker — that are not a known fund.
  Their ticker, spelled the SEC's way, finds the CIK; a non-US listing, or a ticker the SEC
  does not list, never reaches companyfacts.
- **The Nasdaq-100** (`universe`): the `UNIVERSE_SIZE` heaviest distinct companies of the
  stored basket of each fund in `UNIVERSE_FUNDS` — QQQM's, which the evening basket refresh
  keeps current, so a reconstitution reaches this list without a code change. Equity rows
  only (whitelisted); a constituent the account does not hold gets a `sec_companies` row and
  never a `securities` one. Each ticker's CIK, or the SEC's "no such ticker", is kept in
  `sec_tickers`. A line published with a CINS and no ISIN takes the ISIN the identity store
  gives its share-class FIGI (Linde, Seagate), so a look-through row can find its page.
- **Named elsewhere** (`named_elsewhere`, review item R5, owner's approval 2026-11-07): the
  look-through's heaviest `NAMED_LOOKTHROUGH` companies with a US ISIN and the watchlist's
  US tickers, on top of the index's hundred. Each full pass stamps the companies it found
  (`universe_seen_at`), which is how the tab lists the current index and not a former member.

A foreign private issuer's annual 20-F counts as an annual report (review item R6): ASML's in
US GAAP, NU's and TSMC's in IFRS (`normaliser.IFRS_TWINS`), annual figures only. A filer
whose document holds nothing this reads (a 40-F filer, a fund) is a miss, backed off like any
other.

**Filing instances** (`edgar_filing_facts`): a quarter with dividends paid and no dividend
per share sends the pass to the filing that reports it, whose XBRL instance may tag the
figure on a dimension member companyfacts leaves out (MA from 2019 to mid-2023). Each filing
is read once ever and what it held is stored (`sec_filing_reads`); at most
`FILING_READS_PER_COMPANY` a company a pass, newest first.

**What the pass checks** (`ttm_dividend_gaps`, `stale_figures` in its record): a dividend
payer whose newest four quarters miss a dividend per share (its TTM is absent now), and a
company whose newest figures are older than `FIGURES_STALE_AFTER` (a filing EDGAR has not
served). A warning names only the companies the previous scheduled run did not list, so a
known gap is a line of the record, not a banner every week. Both lists are read from the
stored rows of **every** company in scope after the pass, fetched or not: a company the
weekday check fetched is not due on Sunday, and building them from the fetched alone left
it out of that Sunday's record — so the Sunday after warned about it again as new (review
items CO2/DR3).

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
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.config import settings
from app.etf_mappings import is_known_etf_isin
from app.models.etf_basket import EtfHolding
from app.models.isin_identity import IsinIdentity
from app.models.security import Security
from app.models.watchlist_item import WatchlistItem
from app.repositories.company_financials_repository import (
    CompanyFinancialsRepository,
    facts_backoff_until,
)
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.repositories.security_repository import held
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.company_financials_normaliser import normalise
from app.services.edgar_client import (
    EdgarClient,
    EdgarError,
    EdgarRefused,
    PeriodicFiling,
    is_configured,
    sec_ticker,
)
from app.services.edgar_filing_facts import (
    Filing,
    covers,
    dividend_gaps,
    filings,
    instance_name,
    parse_instance,
    sources,
    ttm_dividend_gap,
)
from app.services.etf_basket_parsers import INVESTED_ASSET_CLASSES
from app.services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)

#: The gate both entry points hold. Not `SYNC_PIPELINE`: EDGAR shares no upstream with
#: Yahoo or IBKR, and entering that gate would put the stock Sync buttons on cooldown.
EDGAR_GATE = "edgar"
SCHEDULED_SYNC_TYPE = "company_financials_refresh"
MANUAL_SYNC_TYPE = "manual_company_financials_fetch"
#: The weekday check's own type: `_new_findings` compares a weekly pass with the previous
#: weekly one, and a check's partial lists must not stand in for it.
CHECK_SYNC_TYPE = "company_filings_check"

#: Under a week, so the weekly pass refetches every company it fetched the week before.
STALE_AFTER = timedelta(days=6)
#: How long a company's industry and fiscal year end (`submissions`) are believed before
#: the pass asks again. They change about never, so not weekly like the figures.
PROFILE_RECHECK_AFTER = timedelta(days=30)
CIK_RECHECK_AFTER = timedelta(days=30)

#: The funds whose stored baskets name the companies stored beyond the held ones: the
#: Nasdaq-100, through QQQM (owner's request, 2026-10-25). Symbols or ISINs.
UNIVERSE_FUNDS = ("QQQM",)
#: Companies, not lines: GOOG and GOOGL are one, so the index's 101 lines are 100.
UNIVERSE_SIZE = 100
#: The look-through's heaviest companies whose US filers the tab carries beside the index
#: (`named_elsewhere`, review item R5; owner's approval 2026-11-07 for about twenty more
#: companies' EDGAR and Yahoo requests a week).
NAMED_LOOKTHROUGH = 100
#: Filing instances read for one company in one pass (two requests each). The first fill
#: reads MA's six years of dividend gaps in about ten, BKR's nine in about twenty; a company
#: that keeps tagging only on a dimension member needs one a quarter after that.
FILING_READS_PER_COMPANY = 24
#: A company's newest figures older than this are a filing EDGAR has not served: a 10-Q is
#: due 40-45 days after its quarter and a 10-K 60-90 days after the year, so the newest
#: period end is at most about 135 days old (a year's end, until the next first quarter).
FIGURES_STALE_AFTER = timedelta(days=150)
#: The weekday check watches a company for its next 10-Q or 10-K from this long after its
#: newest stored period end — the quarter (about 91 days) plus the two weeks the quickest
#: filers take — until `FILING_WATCH_UNTIL`; past that only the weekly pass asks, so a
#: company that stopped filing is not asked about every day.
FILING_EXPECTED_AFTER = timedelta(days=105)
FILING_WATCH_UNTIL = FIGURES_STALE_AFTER + timedelta(days=30)
#: Past the watch, a company's figures are stale: its filing may be listed while
#: companyfacts never took it in (six companies sat on their Q1 for 221 days, review item
#: CO5). The check asks such a company once a week, on `CHASE_WEEKDAY`, until this old —
#: past a year of missing 10-Qs it has stopped filing.
FILING_CHASE_UNTIL = timedelta(days=400)
CHASE_WEEKDAY = 0  # Monday
#: A company fetched within this is not checked: the Sunday pass, or a check that found it.
CHECKED_WITHIN = timedelta(hours=20)
#: A filing's period counts as new only this far past the stored one. A quarter is ~91 days
#: on; a `reportDate` a few days off the stored period end must not refetch every day.
NEW_PERIOD_MARGIN = timedelta(days=14)

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
    # Counted against `UNIVERSE_SIZE`: the index's lines. The companies the look-through and
    # the watchlist name (`named_elsewhere`) are not; they come on top.
    capped: bool = True


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
        The universe funds' equity constituents, heaviest first, one line per ticker, then
        the companies named elsewhere (`named_elsewhere`); and the funds that have no stored
        basket.
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
        isin_of_figi = await self._isins_of_figis(lines)
        out: Dict[str, Constituent] = {}
        for row in sorted(lines, key=lambda r: -r.weight_pct):
            # Whitelisted: a money-market fund, a future or cash is no company.
            if (row.asset_class or "").strip().lower() not in INVESTED_ASSET_CLASSES:
                continue
            if not row.constituent_ticker or is_known_etf_isin(row.constituent_isin):
                continue
            ticker = sec_ticker(row.constituent_ticker)
            isin = row.constituent_isin or isin_of_figi.get(
                row.constituent_share_class_figi
            )
            out.setdefault(ticker, Constituent(ticker, row.constituent_name, isin))
        return list(out.values()) + await self.named_elsewhere(), missing

    async def _isins_of_figis(self, lines: Sequence[EtfHolding]) -> Dict[str, str]:
        """
        A share-class FIGI -> the ISIN the identity store gives it, for the lines published
        with a CINS and no ISIN (review item R5): QQQM lists Linde and Seagate that way, so
        their company rows carried no ISIN and a look-through row of Linde, which only an ISIN
        names, linked nowhere. The alphabetically first where several ISINs share one.
        """
        figis = {
            row.constituent_share_class_figi
            for row in lines
            if not row.constituent_isin and row.constituent_share_class_figi
        }
        if not figis:
            return {}
        found = await self.db.execute(
            select(IsinIdentity.share_class_figi, IsinIdentity.isin)
            .where(IsinIdentity.share_class_figi.in_(sorted(figis)))
            .order_by(IsinIdentity.isin)
        )
        out: Dict[str, str] = {}
        for figi, isin in found:
            out.setdefault(figi, isin)
        return out

    async def named_elsewhere(self) -> List[Constituent]:
        """
        The companies the app names outside the index, as constituents `UNIVERSE_SIZE` does
        not count (review item R5, 2026-11-08): **the look-through's heaviest
        `NAMED_LOOKTHROUGH`** with a US ISIN, under the ticker the stored baskets most often
        give that ISIN, and **the watchlist's US tickers** (no exchange suffix). 37 of the
        look-through's top 100 rows linked nowhere. A US ISIN, not any ticker the SEC lists:
        a foreign line's ticker can name another company there (Roche's ROG is Rogers
        Corp's). A held company is in scope already; one named twice is one company.
        """
        from app.services.lookthrough_service import LookthroughService

        view = await LookthroughService(self.db).get_lookthrough(limit=NAMED_LOOKTHROUGH)
        wanted: List[Tuple[str, str]] = []
        for row in view.get("companies") or []:
            isin = next(
                (i for i in sorted(row.get("isins") or []) if i.upper().startswith("US")),
                None,
            )
            if isin is not None:
                wanted.append((isin.upper(), row.get("name") or isin))
        counts: Dict[str, Counter] = defaultdict(Counter)
        if wanted:
            found = await self.db.execute(
                select(EtfHolding.constituent_isin, EtfHolding.constituent_ticker).where(
                    EtfHolding.constituent_isin.in_([isin for isin, _ in wanted]),
                    EtfHolding.constituent_ticker.is_not(None),
                )
            )
            for isin, ticker in found:
                counts[isin.upper()][sec_ticker(ticker)] += 1
        out: List[Constituent] = []
        for isin, name in wanted:
            if counts[isin]:
                ticker = max(counts[isin].items(), key=lambda kv: (kv[1], kv[0]))[0]
                out.append(Constituent(ticker, name, isin, capped=False))
        items = (await self.db.execute(select(WatchlistItem))).scalars()
        for item in items:
            symbol = (item.yahoo_ticker or "").strip().upper()
            if not symbol or any(mark in symbol for mark in (".", "^", "=")):
                continue
            ticker = sec_ticker(symbol)
            out.append(Constituent(ticker, item.company_name or ticker, None, capped=False))
        return out

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
            "profiles_due": 0,
            "profiles_fetched": 0,
            "profile_errors": 0,
            "filings_read": 0,
            "filing_errors": 0,
            "filings_unreadable": 0,
            "ttm_dividend_gaps": [],
            "stale_figures": [],
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
            summary["profiles_due"] = len(await self._profiles_due(targets, now, force))
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
            # The lists below read every target's stored rows; none when the ticker list
            # was refused before the targets were known.
            targets: List[Target] = []
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
                    outcome = await self._one(
                        client, target.cik, summary, target.symbols[0]
                    )
                    summary["outcomes"].append(
                        {
                            "cik": target.cik,
                            "symbols": target.symbols,
                            "outcome": outcome,
                        }
                    )
                # Then the industry and fiscal year end, at most monthly a company. After
                # the figures, so a refusal there leaves the figures' pass whole.
                profiles = await self._profiles_due(targets, now, force)
                summary["profiles_due"] = len(profiles)
                for target in profiles:
                    await self._profile(client, target.cik, summary)
            except EdgarRefused as refused:
                summary["stopped"] = refused.status
                hint = "" if refused.status == 429 else " Check EDGAR_USER_AGENT."
                warnings.append(
                    f"The SEC answered HTTP {refused.status}; the company financials "
                    f"pass stopped and the rest waits for the next one.{hint}"
                )
            await self._findings(targets, summary)

        failed = summary["errors"]
        n = sum(1 for o in summary["outcomes"] if o["outcome"].startswith("error"))
        if n:
            warnings.append(
                f"{n} compan{'y' if n == 1 else 'ies'} could not be fetched from EDGAR "
                f"and keep their previous figures."
            )
        p = summary["profile_errors"]
        if p:
            warnings.append(
                f"The industry of {p} compan{'y' if p == 1 else 'ies'} could not be read "
                f"from EDGAR; asked again next week."
            )
        warnings.extend(await self._new_findings(summary))
        warnings.extend(_unchained_warning(summary))
        f = summary["filing_errors"]
        if f:
            warnings.append(
                f"{f} filing{'' if f == 1 else 's'} could not be read from EDGAR for a "
                f"missing dividend per share; asked again next week."
            )
        u = summary["filings_unreadable"]
        if u:
            warnings.append(
                f"{u} filing instance{'' if u == 1 else 's'} EDGAR served could not be "
                f"parsed; recorded as holding nothing."
            )
        summary["message"] = (
            f"Company financials: {summary['fetched']} of {summary['companies_due']} "
            f"companies fetched, {summary['rows_written']} rows, "
            f"{summary['no_data']} without 10-Q/10-K figures, {failed} failed, "
            f"{summary['filings_read']} filings read"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def check_filings(self) -> Dict[str, Any]:
        """
        The weekday check. For each company in scope whose next filing is due
        (`_filing_due`), one `submissions` request for the period its newest 10-Q or 10-K
        reports; a later one than stored fetches the company through `_one`, exactly as the
        weekly pass does. Pure database until then: no ticker list, no CIK lookups (the
        weekly pass keeps those). Returns a summary for a `sync_runs` row.
        """
        if not is_configured():
            return {
                "status": "skipped",
                "message": "Company filings: skipped, EDGAR_USER_AGENT is not set",
                "warnings": [NOT_CONFIGURED_WARNING],
            }
        now = utcnow()
        securities = await self.in_scope()
        constituents, _ = await self.universe()
        stamps = await self.repo.cik_rows(s.id for s in securities)
        known, _ = await self._known_tickers()
        due = await self._filing_due(
            self._targets(securities, stamps, constituents, known), now
        )
        summary: Dict[str, Any] = {
            "status": "success",
            "watching": len(due),
            "checked": 0,
            "new_filings": 0,
            "fetched": 0,
            "rows_written": 0,
            "no_data": 0,
            "errors": 0,
            "stopped": None,
            "outcomes": [],
            "filings_read": 0,
            "filing_errors": 0,
            "filings_unreadable": 0,
            "ttm_dividend_gaps": [],
            "stale_figures": [],
        }
        warnings: List[str] = []
        fetched: List[Target] = []
        async with EdgarClient(
            settings.edgar_user_agent.strip(),
            transport=self._transport,
            sleep=self._sleep,
        ) as client:
            try:
                for target, stored_end in due:
                    try:
                        reported = await client.latest_report(target.cik)
                    except EdgarError as e:
                        summary["errors"] += 1
                        logger.warning(f"submissions for CIK {target.cik} failed: {e}")
                        continue
                    summary["checked"] += 1
                    if (
                        reported is None
                        or reported.period_end < stored_end + NEW_PERIOD_MARGIN
                    ):
                        continue
                    summary["new_filings"] += 1
                    company = (await self.repo.companies([target.cik])).get(target.cik)
                    misses = company.facts_miss_count if company is not None else None
                    wrote = summary["fetched"]
                    outcome = await self._one(
                        client, target.cik, summary, target.symbols[0], expected=reported
                    )
                    summary["outcomes"].append(
                        {"cik": target.cik, "symbols": target.symbols, "outcome": outcome}
                    )
                    fetched.append(target)
                    if summary["fetched"] > wrote:
                        await self._lagging(target, reported, misses, summary)
            except EdgarRefused as refused:
                summary["stopped"] = refused.status
                hint = "" if refused.status == 429 else " Check EDGAR_USER_AGENT."
                warnings.append(
                    f"The SEC answered HTTP {refused.status}; the filings check stopped "
                    f"and the rest waits for the next one.{hint}"
                )
        # The check's own lists cover what it fetched; the Sunday pass's cover everyone.
        await self._findings(fetched, summary)
        warnings.extend(_unchained_warning(summary))
        failed = sum(1 for o in summary["outcomes"] if o["outcome"].startswith("error"))
        if failed:
            warnings.append(
                f"{failed} compan{'y' if failed == 1 else 'ies'} with a new filing could "
                f"not be fetched from EDGAR and keep their previous figures."
            )
        summary["message"] = (
            f"Company filings: {summary['checked']} of {summary['watching']} due checked, "
            f"{summary['new_filings']} new, {summary['fetched']} fetched"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _lagging(
        self,
        target: Target,
        reported: PeriodicFiling,
        misses: Optional[int],
        summary: Dict[str, Any],
    ) -> None:
        """
        After a fetch the filing list said was due: when the stored figures still stop
        short of the reported period, companyfacts lags the SEC's own filing list, and the
        next ask waits on the shared doubling (`facts_backoff_until`), counted across such
        fetches — the write cleared the stamps. Asking daily cost about sixty fetches a
        filing that never moved anything (review item CO5).
        """
        newest = ((await self.repo.coverage([target.cik])).get(target.cik) or {}).get(
            "last"
        )
        if newest is not None and reported.period_end < newest + NEW_PERIOD_MARGIN:
            return
        await self.repo.record_lag(target.cik, utcnow(), misses)
        await self.db.commit()
        summary.setdefault("lagging", []).append(target.symbols[0])

    async def _filing_due(
        self, targets: Sequence[Target], now: datetime
    ) -> List[Tuple[Target, Any]]:
        """
        The targets whose next 10-Q or 10-K is due, each with its newest stored period end:
        figures stored, the newest between `FILING_EXPECTED_AFTER` and `FILING_WATCH_UNTIL`
        old — or, on a Monday, up to `FILING_CHASE_UNTIL` (review item CO5) — not backed
        off, not fetched within `CHECKED_WITHIN`. A company with no figures is the weekly
        pass's.
        """
        ciks = [t.cik for t in targets]
        companies = await self.repo.companies(ciks)
        coverage = await self.repo.coverage(ciks)
        today = utc_today()
        due: List[Tuple[Target, Any]] = []
        for target in targets:
            newest = (coverage.get(target.cik) or {}).get("last")
            if newest is None:
                continue
            age = today - newest
            watched = FILING_EXPECTED_AFTER <= age <= FILING_WATCH_UNTIL
            chased = (
                today.weekday() == CHASE_WEEKDAY
                and FILING_WATCH_UNTIL < age <= FILING_CHASE_UNTIL
            )
            if not (watched or chased):
                continue
            company = companies.get(target.cik)
            wait = facts_backoff_until(company)
            if wait is not None and wait > now:
                continue
            if (
                company is not None
                and company.fetched_at is not None
                and company.fetched_at > now - CHECKED_WITHIN
            ):
                continue
            due.append((target, newest))
        return due

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
        """The first `UNIVERSE_SIZE` distinct companies among the index's constituents,
        then every company named elsewhere (`named_elsewhere`) not among them."""
        ciks: List[int] = []
        capped = 0
        for constituent in constituents:
            cik = known.get(constituent.ticker)
            if cik is None or cik in ciks:
                continue
            if constituent.capped:
                if capped == UNIVERSE_SIZE:
                    continue
                capped += 1
            ciks.append(cik)
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

    async def _profiles_due(
        self, targets: Sequence[Target], now: datetime, force: bool
    ) -> List[Target]:
        """The targets whose industry was never asked, or not within `PROFILE_RECHECK_AFTER`."""
        if force:
            return list(targets)
        companies = await self.repo.companies(t.cik for t in targets)
        due: List[Target] = []
        for target in targets:
            company = companies.get(target.cik)
            stamp = company.submissions_checked_at if company is not None else None
            if stamp is None or stamp <= now - PROFILE_RECHECK_AFTER:
                due.append(target)
        return due

    async def _profile(
        self, client: EdgarClient, cik: int, summary: Dict[str, Any]
    ) -> None:
        """One company's submissions document; a failure stamps nothing (next week asks)."""
        try:
            profile = await client.submissions(cik)
        except EdgarError as e:
            summary["profile_errors"] += 1
            logger.warning(f"submissions for CIK {cik} failed: {e}")
            return
        await self.repo.record_profile(cik, profile, utcnow())
        await self.db.commit()
        summary["profiles_fetched"] += 1

    async def _findings(self, targets: Sequence[Target], summary: Dict[str, Any]) -> None:
        """`ttm_dividend_gaps` and `stale_figures` over `targets`' stored rows, whether
        this pass fetched them or not (see the module docstring)."""
        today = utc_today()
        for target in targets:
            rows = await self.repo.finding_rows(target.cik)
            if not rows:
                continue
            label = target.symbols[0]
            if ttm_dividend_gap(rows):
                summary["ttm_dividend_gaps"].append(label)
            if today - max(r["period_end"] for r in rows) > FIGURES_STALE_AFTER:
                summary["stale_figures"].append(label)

    async def _new_findings(self, summary: Dict[str, Any]) -> List[str]:
        """Warnings for the companies this pass found with a broken TTM dividend per share
        or stale figures that the previous scheduled run did not list."""
        previous = await SyncRunRepository(self.db).get_latest(
            sync_type=SCHEDULED_SYNC_TYPE
        )
        # The scheduled job records this summary under `company_financials_result`.
        details = (previous.details or {}) if previous is not None else {}
        before = details.get("company_financials_result") or {}

        def new_since(key: str) -> List[str]:
            return [s for s in summary[key] if s not in (before.get(key) or [])]

        out = []
        new = new_since("ttm_dividend_gaps")
        if new:
            out.append(
                f"No TTM dividend per share for {', '.join(new)}: a quarter of the newest "
                f"four has dividends paid and no dividend per share EDGAR gives."
            )
        new = new_since("stale_figures")
        if new:
            out.append(
                f"The newest figures of {', '.join(new)} are more than "
                f"{FIGURES_STALE_AFTER.days} days old: EDGAR has not served a newer 10-Q or "
                f"10-K."
            )
        return out

    async def _one(
        self,
        client: EdgarClient,
        cik: int,
        summary: Dict[str, Any],
        label: str,
        expected: Optional[PeriodicFiling] = None,
    ) -> str:
        try:
            facts = await client.company_facts(cik)
        except EdgarError as e:
            summary["errors"] += 1
            logger.warning(f"companyfacts for CIK {cik} failed: {e}")
            return f"error: {e}"
        stored = await self.repo.filing_reads(cik) if facts is not None else {}
        result = normalise(facts, _filing_facts(stored)) if facts is not None else None
        if result is None or not result.has_data:
            await self.repo.record_miss(cik, utcnow())
            await self.db.commit()
            summary["no_data"] += 1
            return "no 10-Q/10-K figures" if facts is not None else "not found"
        refused: Optional[EdgarRefused] = None
        newest = _filing_behind(result.rows, expected, set(stored))
        if newest is not None:
            # companyfacts has not taken in the filing the SEC lists: its own instance
            # gives its periods (review item CO5), read once ever like any other.
            try:
                found = await self._read_filing(client, cik, newest, summary, whole=True)
            except EdgarRefused as e:
                refused = e
            except EdgarError as e:
                summary["filing_errors"] += 1
                logger.warning(f"filing {newest.accn} of CIK {cik} failed: {e}")
            else:
                await self.repo.record_filing_read(
                    cik, newest.accn, newest.form, newest.filed, found, utcnow()
                )
                await self.db.commit()
                summary["filings_read"] += 1
                stored = await self.repo.filing_reads(cik)
                result = normalise(facts, _filing_facts(stored))
        read = 0
        if refused is None:
            read, refused = await self._read_filings(
                client, cik, facts, result.rows, set(stored), summary
            )
        if read:
            result = normalise(facts, _filing_facts(await self.repo.filing_reads(cik)))
        await self.repo.replace_rows(
            cik, result.rows, utcnow(), result.entity_name, result.splits
        )
        if result.unchained_investments:
            summary.setdefault("unchained_investments", []).append(
                f"{label} ({', '.join(result.unchained_investments)})"
            )
        await self.db.commit()
        summary["fetched"] += 1
        summary["rows_written"] += len(result.rows)
        if refused is not None:
            # The figures are written; the refusal still stops the pass.
            raise refused
        return f"{len(result.rows)} rows"

    async def _read_filings(
        self,
        client: EdgarClient,
        cik: int,
        companyfacts: Dict[str, Any],
        rows: Sequence[dict],
        read: set,
        summary: Dict[str, Any],
    ) -> Tuple[int, Optional[EdgarRefused]]:
        """
        The filings behind this company's dividend gaps, newest first, each read once ever
        and at most `FILING_READS_PER_COMPANY` this pass. A gap asks its `sources` in turn —
        its own filing, then the ones that repeat it — until one holds it; a gap the filings
        already read this pass cover (a comparative quarter, a year) is not asked for again.
        Returns how many were read, and the refusal that stopped them, if one did. A failure
        stamps nothing and stops this company's reads: next week asks again.
        """
        by_accn = filings(companyfacts)
        pending = dividend_gaps(rows)
        count = 0
        while pending and count < FILING_READS_PER_COMPANY:
            gap = pending.pop(0)
            for filing in sources(by_accn, gap):
                if filing.accn in read:
                    continue
                if count >= FILING_READS_PER_COMPANY:
                    break
                try:
                    found = await self._read_filing(client, cik, filing, summary)
                except EdgarRefused as refused:
                    return count, refused
                except EdgarError as e:
                    summary["filing_errors"] += 1
                    logger.warning(f"filing {filing.accn} of CIK {cik} failed: {e}")
                    return count, None
                await self.repo.record_filing_read(
                    cik, filing.accn, filing.form, filing.filed, found, utcnow()
                )
                await self.db.commit()
                read.add(filing.accn)
                count += 1
                summary["filings_read"] += 1
                pending = [g for g in pending if not covers(found, g)]
                if covers(found, gap):
                    break
        return count, None

    async def _read_filing(
        self,
        client: EdgarClient,
        cik: int,
        filing: Filing,
        summary: Dict[str, Any],
        whole: bool = False,
    ) -> List[dict]:
        """What one filing's instance holds; nothing when it has none, or none EDGAR lists.
        `whole`: its undimensioned chain facts too (`parse_instance`)."""
        index = await client.filing_index(cik, filing.accn)
        name = instance_name(index) if index is not None else None
        if name is None:
            return []
        body = await client.filing_document(cik, filing.accn, name)
        if body is None:
            return []
        try:
            return parse_instance(body, whole=whole)
        except ET.ParseError as e:
            summary["filings_unreadable"] += 1
            logger.warning(f"filing {filing.accn} of CIK {cik}: {name} is not XML ({e})")
            return []


def _filing_behind(
    rows: Sequence[dict], expected: Optional[PeriodicFiling], read: set
) -> Optional[Filing]:
    """
    The filing the SEC lists that companyfacts has not taken in (review item CO5): the
    newest periodic report is `NEW_PERIOD_MARGIN` past the newest period `rows` reach,
    and its accession number and filing date are known and it was never read. None
    otherwise.
    """
    if expected is None or expected.accn is None or expected.filed is None:
        return None
    if expected.accn in read:
        return None
    newest = max((r["period_end"] for r in rows), default=None)
    if newest is not None and expected.period_end < newest + NEW_PERIOD_MARGIN:
        return None
    return Filing(expected.accn, expected.form, expected.filed, expected.period_end)


def _unchained_warning(summary: Dict[str, Any]) -> List[str]:
    """The companies this pass wrote whose net cash counts cash alone while their filings
    carry a current-investments concept no chain reads (review item CO8)."""
    found = summary.get("unchained_investments") or []
    if not found:
        return []
    return [
        f"Short-term investments outside the concept chain for {', '.join(found)}: "
        f"net cash counts cash alone until the chain names the concept."
    ]


def _filing_facts(stored: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The stored reads as the normaliser takes them: each fact with its filing's
    accession number, form and filing date."""
    return [
        {**fact, "accn": row.accn, "form": row.form, "filed": row.filed.isoformat()}
        for row in stored.values()
        for fact in row.facts or ()
    ]
