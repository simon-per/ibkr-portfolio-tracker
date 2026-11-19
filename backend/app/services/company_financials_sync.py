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
  A look-through company **without** a US ISIN (an Irish 10-K filer: Eaton, Accenture) is
  found by the baskets' ticker only when the SEC's name for that ticker agrees with the
  company's (`names_agree`) and the SEC filer reports on 10-K (`annual_form`): Roche's ROG is
  Rogers Corp's ticker there, Schneider's SU Suncor's (2026-11-17). **Or on 20-F**
  (2026-11-19, owner: "use SEC for SAP etc. where it exists"), while it still files
  (`ANNUAL_CURRENT_WITHIN`) and no other regulator's page has the company: priced by its
  ordinary share, never at Yahoo (`repo.ordinary_priced_ciks`). A basket ticker the SEC
  lists under no agreeing name sends the company's name over the whole ticker list
  (`_search_names`, `SecNameMatch`): Novartis's NOVN finds NVS, Toyota's 7203 TM.

A foreign private issuer's annual 20-F counts as an annual report (review item R6): ASML's in
US GAAP, NU's and TSMC's in IFRS (`normaliser.IFRS_TWINS`), annual figures only. A filer
whose document holds nothing this reads (a 40-F filer, a fund) is a miss, backed off like any
other.

**Filing instances** (`edgar_filing_facts`): a quarter with dividends paid and no dividend
per share, or with net income and no diluted EPS, sends the pass to the filing that reports
it, whose XBRL instance may tag the figure on a dimension member companyfacts leaves out (MA's
dividend from 2019 to mid-2023, BKR's EPS from 2023). Each filing is read once for what the
reader looks for and what it held is stored (`sec_filing_reads`); at most
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
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
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
from app.services.company_classification import classify, statement_model
from app.services.company_financials_normaliser import QUARTER_WITNESSES, normalise
from app.services.company_vendor_fill import fill as vendor_fill
from app.services.edgar_client import (
    ANNUAL_FORM_PREFIXES,
    FINANCIAL_SIC,
    FOREIGN_ANNUAL_PREFIXES,
    EdgarClient,
    EdgarError,
    EdgarRefused,
    PeriodicFiling,
    is_configured,
    sec_ticker,
)
from app.services.edgar_filing_facts import (
    GAP_CONCEPTS,
    READ_SINCE,
    READER,
    SKIPPED_LOOKBACK,
    Filing,
    companyfacts_accns,
    covers,
    debt_blind,
    extension_gaps,
    extension_metrics,
    filings,
    gaps,
    instance_name,
    label_linkbase,
    parse_instance,
    read_period,
    segment_gaps,
    skipped,
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
#: companies' EDGAR and Yahoo requests a week; 100 -> 200 on 2026-11-17, owner's approval
#: for about thirty more companies' weekly closes and profile rotation).
NAMED_LOOKTHROUGH = 200
#: The annual report a company found by its name must file: a 10-K, or a 20-F (2026-11-19).
#: A 20-F filer listed in New York through depositary receipts quotes a receipt that can
#: stand for ten shares (Toyota) or two (BHP) beside an EPS per ordinary share, so it is
#: priced by its ordinary share (`repo.ordinary_priced_ciks`); and where another regulator
#: already gives it a page (Shell, Santander: ESEF) that page stays (`_resolve`).
NAME_MATCH_FORMS = ("10-K",) + FOREIGN_ANNUAL_PREFIXES
#: A successor without an annual report is asked for its predecessor at most this often,
#: through at most this many other filers' CIKs its periodic reports carry
#: (`_find_predecessor`).
PREDECESSOR_RECHECK_AFTER = timedelta(days=30)
PREDECESSOR_CANDIDATES = 3
#: A filer whose newest annual report ends longer ago than this has stopped filing at the
#: SEC (ABB's last 20-F is FY2023, Infineon's FY2009): a year, the four months a 20-F may
#: take, and slack. Its page would set today's price beside old figures.
ANNUAL_CURRENT_WITHIN = timedelta(days=550)
#: Past this since the recorded annual report's end, the next one is due: its filing list is
#: asked again, at most once in `CIK_RECHECK_AFTER` (`form_due`).
ANNUAL_REPORT_DUE = timedelta(days=400)
#: Filing instances read for one company in one pass (two requests each). The first fill
#: reads MA's six years of dividend gaps in about ten, BKR's nine in about twenty; a company
#: that keeps tagging only on a dimension member needs one a quarter after that.
FILING_READS_PER_COMPANY = 24
#: A company's newest figures older than this are a filing EDGAR has not served: a 10-Q is
#: due 40-45 days after its quarter and a 10-K 60-90 days after the year, so the newest
#: period end is at most about 135 days old (a year's end, until the next first quarter).
FIGURES_STALE_AFTER = timedelta(days=150)
#: An annual-only filer's (a 20-F or 40-F) newest figures are a year's: its next report is due
#: four months after the next year end, so a year and five months (2026-11-19: the seven 20-F
#: filers read that day were all named stale under the quarterly bound).
ANNUAL_FIGURES_STALE_AFTER = timedelta(days=365 + 150)
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
#: How long one ask of a company's filing list for a filing companyfacts skipped
#: (`_read_skipped`) stands: a hole no listed filing fills — a new filer's comparative
#: quarters, a company that stopped filing — costs one small request a month.
LISTING_RECHECK_AFTER = timedelta(days=30)

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
    # A company without a US ISIN, found by the baskets' ticker: it resolves only when the
    # SEC's name for the ticker agrees with one of `names` (`names_agree`) and the filer
    # files a current 10-K or 20-F (`NAME_MATCH_FORMS`, `annual_current`).
    name_check: bool = False
    names: Tuple[str, ...] = ()
    # Every ISIN the company is known by (a name-checked one's): another regulator's page
    # for any of them keeps a 20-F filer out.
    isins: Tuple[str, ...] = ()


#: Legal forms and share-class words, dropped from the end of a name before two names are
#: compared (`name_tokens`). A standard's words, never a company's.
LEGAL_FORMS = frozenset(
    {
        "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
        "plc", "public", "llc", "lp", "ag", "sa", "se", "nv", "bv", "spa", "sab", "cv",
        "de", "oyj", "asa", "kgaa", "gmbh", "aktiengesellschaft", "the", "adr", "ads",
        "ord", "shs", "reg", "n",
    }
)
_CLASS = re.compile(r"\b(?:class|cl)\s+[a-z]\b")
_PARENS = re.compile(r"\([^)]*\)")
#: A basket name this long may be cut short (`JOHNSON CONTROLS INTERNATION`).
TRUNCATED_NAME_LENGTH = 24


def name_tokens(name: str) -> List[str]:
    """
    A company name for comparison: accents and case dropped, dots of abbreviations closed
    (S.A. is SA), `&` as `and`, parentheticals and share classes (`Class A`, `CL A`) removed,
    then legal forms dropped from the end (`Corp plc`, `public limited company`, `AG`).
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _PARENS.sub(" ", text).replace(".", "").replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = _CLASS.sub(" ", text).split()
    while tokens and tokens[-1] in LEGAL_FORMS:
        tokens.pop()
    return tokens


def names_agree(names: Sequence[str], title: Optional[str]) -> bool:
    """
    Whether the SEC's name for a ticker (`title`) is one of `names` (the look-through's and
    the baskets'), under `name_tokens`. Equal tokens agree; so does a basket name long
    enough to have been cut short (`TRUNCATED_NAME_LENGTH`) whose tokens begin the SEC's,
    its last token a prefix of the SEC's same token — two tokens at least. Nothing else:
    Roche Holding is not Rogers, Schneider Electric not Suncor.
    """
    if not title:
        return False
    sec = name_tokens(title)
    if not sec:
        return False
    return any(tokens_agree(name, name_tokens(name), title, sec) for name in names)


def tokens_agree(name: str, ours: List[str], title: str, sec: List[str]) -> bool:
    """`names_agree` for one name, over tokens already taken (`name_tokens`), so a name can
    be held against the whole ticker list (`_search_names`)."""
    if not ours or not sec:
        return False
    if ours == sec:
        return True
    short, long_ = (ours, sec) if len(ours) <= len(sec) else (sec, ours)
    raw = name if short is ours else title
    return (
        len(raw.strip()) >= TRUNCATED_NAME_LENGTH
        and len(short) >= 2
        and short[:-1] == long_[: len(short) - 1]
        and long_[len(short) - 1].startswith(short[-1])
    )


def annual_current(row: Any, today: date) -> bool:
    """
    Whether a ticker's filer files the annual report a company found by its name must
    (`NAME_MATCH_FORMS`) and still files it (`ANNUAL_CURRENT_WITHIN`). A 10-K recorded
    before the period was kept (2026-11-19) stands until its list is asked again.
    """
    form = row.annual_form or ""
    if not form.startswith(NAME_MATCH_FORMS):
        return False
    end = row.annual_period_end
    if end is None:
        return form.startswith("10-K")
    return today - end <= ANNUAL_CURRENT_WITHIN


def form_due(row: Any, now: datetime) -> bool:
    """Whether a ticker's annual report is asked (`_check_forms`): never asked; or asked
    before its period was kept, or the next report is due (`ANNUAL_REPORT_DUE`), and not
    within `CIK_RECHECK_AFTER`. A filer that lists none (`-`) is asked again yearly."""
    if row.annual_form is None:
        return True
    if row.annual_form == "-":
        # None listed: asked again yearly, and once when recorded before the 40-F counted
        # (RBC, TD and Thomson Reuters were "-" until 2026-11-19).
        return row.form_checked_at is None or row.form_checked_at <= now - ANNUAL_REPORT_DUE
    if row.form_checked_at is not None and row.form_checked_at > now - CIK_RECHECK_AFTER:
        return False
    end = row.annual_period_end
    return end is None or now.date() - end > ANNUAL_REPORT_DUE


def apply_name_matches(
    constituents: Sequence[Constituent], matches: Dict[str, Any]
) -> List[Constituent]:
    """A company found by its name takes the SEC's ticker of the filer its name found over
    the whole list (`SecNameMatch`), when its basket ticker found none: NOVN is NVS."""
    out: List[Constituent] = []
    for c in constituents:
        match = matches.get(c.isin) if c.name_check and c.isin else None
        if match is not None and match.cik is not None and match.ticker:
            c = replace(c, ticker=match.ticker)
        out.append(c)
    return out


def has_annual_report(companyfacts: Optional[Dict[str, Any]]) -> bool:
    """Whether a companyfacts document holds any fact an annual report (10-K, 20-F, 40-F)
    states, in us-gaap or ifrs-full."""
    facts = (companyfacts or {}).get("facts") or {}
    for taxonomy in ("us-gaap", "ifrs-full"):
        for body in (facts.get(taxonomy) or {}).values():
            for rows in (body.get("units") or {}).values():
                if any(str(r.get("form", "")).startswith(ANNUAL_FORM_PREFIXES) for r in rows):
                    return True
    return False


def merge_facts(newer: Dict[str, Any], older: Dict[str, Any]) -> Dict[str, Any]:
    """
    A successor's companyfacts with its predecessor's facts beneath, concept by concept and
    unit by unit: one document the normaliser reads as one company. A fact both carry (a
    report they filed jointly) is the same fact twice, and the normaliser keeps one per
    period and filing. The successor's name and CIK stand.
    """
    merged: Dict[str, Any] = {k: v for k, v in newer.items() if k != "facts"}
    facts: Dict[str, Any] = {}
    for doc in (older, newer):
        for taxonomy, concepts in ((doc or {}).get("facts") or {}).items():
            into = facts.setdefault(taxonomy, {})
            for concept, body in (concepts or {}).items():
                target = into.setdefault(concept, {**body, "units": {}})
                for unit, rows in (body.get("units") or {}).items():
                    target["units"].setdefault(unit, []).extend(rows or [])
    merged["facts"] = facts
    return merged


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
        # (the ISINs whose basket lines name the ticker, the company's name, a US ISIN?)
        wanted: List[Tuple[List[str], str, bool]] = []
        for row in view.get("companies") or []:
            isins = sorted({i.upper() for i in row.get("isins") or [] if i})
            us = [i for i in isins if i.startswith("US")]
            if us:
                wanted.append((us[:1], row.get("name") or us[0], True))
            elif isins:
                wanted.append((isins, row.get("name") or isins[0], False))
        counts: Dict[str, Counter] = defaultdict(Counter)
        names: Dict[str, Counter] = defaultdict(Counter)
        every = sorted({isin for isins, _, _ in wanted for isin in isins})
        if every:
            found = await self.db.execute(
                select(
                    EtfHolding.constituent_isin,
                    EtfHolding.constituent_ticker,
                    EtfHolding.constituent_name,
                ).where(
                    EtfHolding.constituent_isin.in_(every),
                    EtfHolding.constituent_ticker.is_not(None),
                )
            )
            for isin, ticker, line_name in found:
                counts[isin.upper()][sec_ticker(ticker)] += 1
                if line_name:
                    names[isin.upper()][line_name.strip()] += 1
        out: List[Constituent] = []
        for isins, name, us in wanted:
            tally: Counter = Counter()
            for isin in isins:
                tally.update(counts[isin])
            if not tally:
                continue
            ticker = max(tally.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if us:
                out.append(Constituent(ticker, name, isins[0], capped=False))
                continue
            line_names = sorted({n for isin in isins for n in names[isin]})
            out.append(
                Constituent(
                    ticker,
                    name,
                    isins[0],
                    capped=False,
                    name_check=True,
                    names=tuple([name, *line_names]),
                    isins=tuple(isins),
                )
            )
        out = apply_name_matches(out, await self.repo.name_matches())
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
        rows = await self.repo.ticker_rows()
        # A ticker asked before and not listed waits `CIK_RECHECK_AFTER`, like a held one.
        # Once per ticker: the index, the look-through and the watchlist can each name it,
        # and two `record_ticker`s of one new ticker are two INSERTs (2026-11-11). A ticker
        # a company is found by its name through needs the SEC's name: asked once more when
        # its row predates the name (`sec_tickers.title`).
        unknown = list(
            {
                c.ticker: c
                for c in constituents
                if (
                    c.ticker not in known
                    and (
                        force
                        or c.ticker not in asked
                        or asked[c.ticker] <= now - CIK_RECHECK_AFTER
                    )
                )
                or (
                    c.name_check
                    and (
                        c.ticker not in rows
                        or (rows[c.ticker].cik is not None and rows[c.ticker].title is None)
                    )
                )
            }.values()
        )
        matches = await self.repo.name_matches()
        searches = self._searches_due(constituents, rows, matches, now, force)
        elsewhere = await self.repo.isins_paged_elsewhere()
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
            "name_checks": 0,
            "name_matched": [],
            "name_searches": 0,
            "name_found": [],
        }
        warnings: List[str] = []
        if missing_baskets:
            warnings.append(
                f"No stored basket for {', '.join(missing_baskets)}; the Nasdaq-100 list "
                f"keeps the companies of the last pass that had one."
            )

        if dry_run:
            resolved = self._resolve(constituents, known, rows, now.date(), elsewhere)
            targets = self._targets(securities, stamps, resolved)
            due = await self._due(targets, now, force)
            summary["universe"] = sum(1 for _, cik in resolved if cik is not None)
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
                if need_cik or unknown or searches:
                    try:
                        listed = await client.company_ticker_list()
                        for security in need_cik:
                            ticker = sec_ticker(security.symbol)
                            entry = listed.get(ticker)
                            await self.repo.record_cik(
                                security.id, entry.cik if entry else None, ticker, now
                            )
                        for constituent in unknown:
                            entry = listed.get(constituent.ticker)
                            cik = entry.cik if entry else None
                            await self.repo.record_ticker(
                                constituent.ticker, cik, now, entry.title if entry else None
                            )
                            if cik is not None:
                                known[constituent.ticker] = cik
                        await self.db.commit()
                        searches = self._searches_due(
                            constituents, await self.repo.ticker_rows(), matches, now, force
                        )
                        await self._search_names(listed, searches, now, summary)
                        await self.db.commit()
                        constituents = apply_name_matches(
                            constituents, await self.repo.name_matches()
                        )
                        for row in (await self.repo.ticker_rows()).values():
                            if row.cik is not None:
                                known.setdefault(row.ticker, row.cik)
                    except EdgarError as e:
                        summary["errors"] += 1
                        warnings.append(
                            f"The SEC ticker list could not be read ({e}); "
                            f"{summary['cik_lookups']} ticker(s) without a CIK wait for "
                            f"the next pass."
                        )
                    stamps = await self.repo.cik_rows(s.id for s in securities)
                rows = await self.repo.ticker_rows()
                await self._check_forms(client, constituents, rows, summary, now)
                resolved = self._resolve(constituents, known, rows, now.date(), elsewhere)
                summary["name_matched"] = sorted(
                    c.ticker for c, cik in resolved if c.name_check and cik is not None
                )
                targets = self._targets(securities, stamps, resolved)
                universe = await self._record_universe(resolved, now, stamp=wanted is None)
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
                f"{f} filing{'' if f == 1 else 's'} could not be read from EDGAR "
                f"(a figure companyfacts leaves out); that company's reads resume next week."
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
        resolved = self._resolve(
            constituents,
            known,
            await self.repo.ticker_rows(),
            now.date(),
            await self.repo.isins_paged_elsewhere(),
        )
        due = await self._filing_due(self._targets(securities, stamps, resolved), now)
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

    async def _with_predecessor(
        self,
        client: EdgarClient,
        cik: int,
        facts: Optional[Dict[str, Any]],
        summary: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        A successor's companyfacts with its predecessor's history beneath (`merge_facts`).
        A filer without an annual report of its own is asked once a month for the filer it
        succeeded (`_find_predecessor`); once found, the predecessor's companyfacts is read
        beside it on every fetch, so its first 10-K does not cut the history off. A failed
        read of either is the company's failure: its stored rows stand.
        """
        if facts is None:
            return facts
        company = (await self.repo.companies([cik])).get(cik)
        if company is None:
            return facts
        predecessor = company.predecessor_cik
        if predecessor is None:
            checked = company.predecessor_checked_at
            if has_annual_report(facts) or (
                checked is not None and checked > utcnow() - PREDECESSOR_RECHECK_AFTER
            ):
                return facts
            predecessor = await self._find_predecessor(client, cik, summary)
            company.predecessor_cik = predecessor
            company.predecessor_checked_at = utcnow()
            await self.db.commit()
            if predecessor is None:
                return facts
        older = await client.company_facts(predecessor)
        return merge_facts(facts, older) if older else facts

    async def _find_predecessor(
        self, client: EdgarClient, cik: int, summary: Dict[str, Any]
    ) -> Optional[int]:
        """
        The filer whose CIK leads the accession number of a periodic report this company's
        filing list holds, when that filer's own filing list holds the same report (one the
        two filed jointly: ExxonMobil Holdings' Q2 2026 10-Q, `0000034088-26-000093`, is
        Exxon Mobil Corp's too) and its companyfacts an annual report. A filing agent's
        prefix (Donnelley's) lists no such report as its own. Companyfacts carries a joint
        report's facts under one registrant only, so the lists are what is compared. At most
        `PREDECESSOR_CANDIDATES` asked.
        """
        filings = await client.periodic_filings(cik) or []
        joint: Dict[int, set] = defaultdict(set)
        for filing in filings:
            prefix = (filing.accn or "").split("-")[0]
            if prefix.isdigit() and int(prefix) != cik:
                joint[int(prefix)].add(filing.accn)
        for candidate in sorted(joint)[:PREDECESSOR_CANDIDATES]:
            listed = await client.periodic_filings(candidate) or []
            if not {f.accn for f in listed} & joint[candidate]:
                continue
            if has_annual_report(await client.company_facts(candidate)):
                summary.setdefault("predecessors", []).append(f"{cik}<-{candidate}")
                return candidate
        return None

    @staticmethod
    def _resolve(
        constituents: Sequence[Constituent],
        known: Dict[str, int],
        rows: Dict[str, Any],
        today: date,
        elsewhere: set,
    ) -> List[Tuple[Constituent, Optional[int]]]:
        """
        Each constituent and its CIK, or None. A company found by its name
        (`Constituent.name_check`) takes the SEC's answer for its ticker only when the SEC's
        name agrees (`names_agree`) and its filer files a current 10-K or 20-F
        (`annual_current`); a 20-F filer not when another regulator's page has any of its
        ISINs (`elsewhere`: Shell's and Santander's ESEF reports stay their pages). Never a
        CIK another table knows the ticker by.
        """
        out: List[Tuple[Constituent, Optional[int]]] = []
        for c in constituents:
            if not c.name_check:
                out.append((c, known.get(c.ticker)))
                continue
            row = rows.get(c.ticker)
            ok = (
                row is not None
                and row.cik is not None
                and names_agree(c.names or (c.name,), row.title)
                and annual_current(row, today)
                and not (
                    (row.annual_form or "").startswith(FOREIGN_ANNUAL_PREFIXES)
                    and elsewhere & set(c.isins or (c.isin,))
                )
            )
            out.append((c, row.cik if ok else None))
        return out

    @staticmethod
    def _searches_due(
        constituents: Sequence[Constituent],
        rows: Dict[str, Any],
        matches: Dict[str, Any],
        now: datetime,
        force: bool,
    ) -> List[Constituent]:
        """
        The companies found by their name whose basket ticker the SEC lists under no
        agreeing name (or not at all), not searched for within `CIK_RECHECK_AFTER`.
        """
        out: List[Constituent] = []
        for c in constituents:
            if not c.name_check or not c.isin:
                continue
            row = rows.get(c.ticker)
            if (
                row is not None
                and row.cik is not None
                and names_agree(c.names or (c.name,), row.title)
            ):
                continue
            match = matches.get(c.isin)
            if (
                match is not None
                and not force
                and match.checked_at > now - CIK_RECHECK_AFTER
            ):
                continue
            out.append(c)
        return out

    async def _search_names(
        self,
        listed: Dict[str, Any],
        searches: Sequence[Constituent],
        now: datetime,
        summary: Dict[str, Any],
    ) -> None:
        """
        Each company's names over the whole ticker list (`names_agree`); exactly one agreeing
        filer is its match, kept by ISIN (`SecNameMatch`) with the filer's first ticker on
        the list (the SEC orders it by size: a filer's main listing). None, or two filers,
        is no match. No request: the list is the one this pass already read.
        """
        if not searches:
            return
        filers: Dict[int, Tuple[str, Optional[str]]] = {}
        for ticker, entry in listed.items():
            filers.setdefault(entry.cik, (ticker, entry.title))
        titled = [
            (cik, t, title, name_tokens(title))
            for cik, (t, title) in filers.items()
            if title
        ]
        for c in searches:
            summary["name_searches"] += 1
            ours = [(n, name_tokens(n)) for n in c.names or (c.name,)]
            hits = [
                (cik, t, title)
                for cik, t, title, sec in titled
                if any(tokens_agree(n, tokens, title, sec) for n, tokens in ours)
            ]
            if len(hits) == 1:
                cik, ticker, title = hits[0]
                await self.repo.record_ticker(ticker, cik, now, title)
                await self.repo.record_name_match(c.isin, cik, ticker, title, now)
                summary["name_found"].append(ticker)
            else:
                await self.repo.record_name_match(c.isin, None, None, None, now)

    async def _check_forms(
        self,
        client: EdgarClient,
        constituents: Sequence[Constituent],
        rows: Dict[str, Any],
        summary: Dict[str, Any],
        now: datetime,
    ) -> None:
        """
        For a company found by its name whose SEC name agrees, the form of its newest
        annual report and the period it ends (the `submissions` document, one small
        request), kept on its ticker's row (`sec_tickers.annual_form`; `-` when it lists
        none) and asked again only when the next one is due (`form_due`).
        """
        for c in constituents:
            row = rows.get(c.ticker)
            if (
                not c.name_check
                or row is None
                or row.cik is None
                or not form_due(row, now)
                or not names_agree(c.names or (c.name,), row.title)
            ):
                continue
            try:
                filings = await client.periodic_filings(row.cik)
            except EdgarError as e:
                logger.warning("EDGAR filing list for %s failed: %s", c.ticker, e)
                continue
            summary["name_checks"] += 1
            annual = next(
                (f for f in filings or [] if f.form.startswith(ANNUAL_FORM_PREFIXES)),
                None,
            )
            if annual is None:
                await self.repo.record_annual_form(c.ticker, "-", None, now)
            else:
                await self.repo.record_annual_form(
                    c.ticker, annual.form, annual.period_end, now
                )
        await self.db.commit()

    def _targets(
        self,
        securities: Sequence[Security],
        stamps,
        resolved: Sequence[Tuple[Constituent, Optional[int]]],
    ) -> List[Target]:
        """Every company in scope, held ones first, then the universe heaviest first."""
        by_cik: Dict[int, Target] = {}
        for security in securities:
            row = stamps.get(security.id)
            if row is not None and row.cik is not None:
                by_cik.setdefault(row.cik, Target(row.cik)).symbols.append(
                    security.symbol
                )
        for cik in self._universe_ciks(resolved):
            target = by_cik.setdefault(cik, Target(cik))
            if not target.symbols:
                target.symbols.append(next(c.ticker for c, k in resolved if k == cik))
        return list(by_cik.values())

    @staticmethod
    def _universe_ciks(resolved: Sequence[Tuple[Constituent, Optional[int]]]) -> List[int]:
        """The first `UNIVERSE_SIZE` distinct companies among the index's constituents,
        then every company named elsewhere (`named_elsewhere`) not among them."""
        ciks: List[int] = []
        capped = 0
        for constituent, cik in resolved:
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
        resolved: Sequence[Tuple[Constituent, Optional[int]]],
        now: datetime,
        stamp: bool,
    ) -> List[int]:
        ciks = self._universe_ciks(resolved)
        first = {}
        for constituent, cik in resolved:
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
            quarterly = any(
                r["fiscal_period"] != "FY" and r["metric"] in QUARTER_WITNESSES for r in rows
            )
            bound = FIGURES_STALE_AFTER if quarterly else ANNUAL_FIGURES_STALE_AFTER
            if today - max(r["period_end"] for r in rows) > bound:
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
            facts = await self._with_predecessor(client, cik, facts, summary)
        except EdgarError as e:
            summary["errors"] += 1
            logger.warning(f"companyfacts for CIK {cik} failed: {e}")
            return f"error: {e}"
        stored = await self.repo.filing_reads(cik) if facts is not None else {}
        result = normalise(facts, _filing_facts(stored)) if facts is not None else None
        extensions = await self._reads_extensions(cik, facts)
        segments = self._reads_segments(facts, result.rows if result is not None else [])
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
                found = await self._read_filing(
                    client, cik, newest, summary, whole=True, extensions=extensions,
                    segments=segments,
                )
            except EdgarRefused as e:
                refused = e
            except EdgarError as e:
                summary["filing_errors"] += 1
                logger.warning(f"filing {newest.accn} of CIK {cik} failed: {e}")
            else:
                await self.repo.record_filing_read(
                    cik, newest.accn, newest.form, newest.filed, found, utcnow(), READER
                )
                await self.db.commit()
                summary["filings_read"] += 1
                stored = await self.repo.filing_reads(cik)
                result = normalise(facts, _filing_facts(stored))
        if refused is None:
            # A filing companyfacts skipped, older than the newest (STATUS 7a): AEP's and
            # EXC's Q2 2026, NU's and TSMC's FY2025 20-F.
            skipped_read, refused = await self._read_skipped(
                client, cik, facts, result.rows, stored, summary, label, extensions,
                segments,
            )
            if skipped_read:
                stored = await self.repo.filing_reads(cik)
                result = normalise(facts, _filing_facts(stored))
        read = 0
        if refused is None:
            read, refused = await self._read_filings(
                client,
                cik,
                facts,
                result.rows,
                {accn: row.reader for accn, row in stored.items()},
                summary,
                extensions,
                segments,
            )
        if read:
            result = normalise(facts, _filing_facts(await self.repo.filing_reads(cik)))
        # Alpha Vantage, last: what it answered before, proven again on these rows
        # (`company_vendor_fill.fill`), so a rewrite keeps it; SEC's rows win.
        rows = result.rows
        statements = await self.repo.vendor_statements(cik)
        if statements:
            rows, _ = vendor_fill(result.rows, statements, utc_today())
        await self.repo.replace_rows(
            cik, rows, utcnow(), result.entity_name, result.splits
        )
        # The classification's first SEC source: what the newest annual report's own concepts
        # show (`company_classification`). A disagreement with the SIC code or SEC's reviewing
        # office is recorded, never resolved by hand — information, not a warning.
        model = statement_model(facts)
        await self.repo.record_statement_model(cik, model)
        company = (await self.repo.companies([cik])).get(cik)
        if company is not None:
            verdict = classify(model, company.sic, company.owner_org)
            for field_name, source, answer in verdict.conflicts:
                summary.setdefault("classification_conflicts", []).append(
                    f"{label} {field_name} {getattr(verdict, field_name)} "
                    f"({getattr(verdict, field_name + '_source')}); {source} says {answer}"
                )
        if result.unchained_investments:
            summary.setdefault("unchained_investments", []).append(
                f"{label} ({', '.join(result.unchained_investments)})"
            )
        # Recorded, not warned: a gap a proven concept filled is the normaliser working
        # (`company_financials_normaliser._bridge`); the record is what audits it.
        for metric, by_concept in sorted(result.bridged.items()):
            for concept, periods in sorted(by_concept.items()):
                summary.setdefault("bridged", []).append(
                    f"{label} {metric} <- {concept} ({periods})"
                )
        # The same for the company's own concepts its filings' labels named (`_extend`)
        # and the debt summed over segment columns (`_segments`).
        for metric, by_concept in sorted(result.segments.items()):
            for concept, periods in sorted(by_concept.items()):
                summary.setdefault("segments", []).append(
                    f"{label} {metric} <- {concept} ({periods})"
                )
        for metric, by_concept in sorted(result.extended.items()):
            for concept, periods in sorted(by_concept.items()):
                summary.setdefault("extended", []).append(
                    f"{label} {metric} <- {concept} ({periods})"
                )
        await self.db.commit()
        summary["fetched"] += 1
        summary["rows_written"] += len(rows)
        if refused is not None:
            # The figures are written; the refusal still stops the pass.
            raise refused
        return f"{len(result.rows)} rows"

    async def _read_skipped(
        self,
        client: EdgarClient,
        cik: int,
        companyfacts: Dict[str, Any],
        rows: Sequence[dict],
        stored: Dict[str, Any],
        summary: Dict[str, Any],
        label: str,
        extensions: bool = False,
        segments: bool = False,
    ) -> Tuple[int, Optional[EdgarRefused]]:
        """
        The filings companyfacts skipped (STATUS 7a), read whole once each. When the figures
        show one (`edgar_filing_facts.skipped`: a recent quarter no filing it names reports,
        a quarter missing between two, figures past due), the filing list (`submissions`)
        is asked — at most once in `LISTING_RECHECK_AFTER`, stamped whatever it holds — and
        every periodic filing it lists within `SKIPPED_LOOKBACK` that companyfacts carries no
        fact of and nothing read yet is read whole (`parse_instance(whole=True)`), newest
        first, at most `FILING_READS_PER_COMPANY`. They only fill, like every filing read.
        Returns how many were read, and the refusal that stopped them, if one did.
        """
        periods = [read_period(row.facts or ()) for row in stored.values()]
        if not skipped(rows, companyfacts, periods, utc_today()):
            return 0, None
        company = (await self.repo.companies([cik])).get(cik)
        asked = company.filings_listed_at if company is not None else None
        now = utcnow()
        if asked is not None and asked > now - LISTING_RECHECK_AFTER:
            return 0, None
        try:
            listed = await client.periodic_filings(cik)
        except EdgarRefused as refused:
            return 0, refused
        except EdgarError as e:
            summary["filing_errors"] += 1
            logger.warning(f"the filing list of CIK {cik} failed: {e}")
            return 0, None
        await self.repo.record_listing(cik, now)
        await self.db.commit()
        summary["filings_listed"] = summary.get("filings_listed", 0) + 1
        known = companyfacts_accns(companyfacts) | set(stored)
        since = utc_today() - SKIPPED_LOOKBACK
        todo = [
            f
            for f in listed or ()
            if f.accn and f.filed and f.accn not in known and f.period_end >= since
        ]
        count = 0
        for listing in todo[:FILING_READS_PER_COMPANY]:
            filing = Filing(listing.accn, listing.form, listing.filed, listing.period_end)
            try:
                found = await self._read_filing(
                    client, cik, filing, summary, whole=True, extensions=extensions,
                    segments=segments,
                )
            except EdgarRefused as refused:
                return count, refused
            except EdgarError as e:
                summary["filing_errors"] += 1
                logger.warning(f"filing {filing.accn} of CIK {cik} failed: {e}")
                return count, None
            await self.repo.record_filing_read(
                cik, filing.accn, filing.form, filing.filed, found, utcnow(), READER
            )
            await self.db.commit()
            count += 1
            summary["filings_read"] += 1
            summary.setdefault("skipped_filings", []).append(
                f"{label} {filing.form} {filing.period_end.isoformat()}"
            )
        return count, None

    def _reads_segments(self, facts: Optional[Dict[str, Any]], rows: Sequence[dict]) -> bool:
        """Whether this company's filings are read for their segment columns' debt: a
        us-gaap filer whose debt no figure shows (`debt_blind`: BRK, PCAR)."""
        gaap = ((facts or {}).get("facts") or {}).get("us-gaap") or {}
        return bool(gaap) and bool(rows) and debt_blind(rows)

    async def _reads_extensions(self, cik: int, facts: Optional[Dict[str, Any]]) -> bool:
        """Whether this company's filings are read for its own capex and debt concepts: a
        us-gaap filer (an IFRS filer's lines are standard ones) that is no bank, broker or
        insurer (`FINANCIAL_SIC`)."""
        gaap = ((facts or {}).get("facts") or {}).get("us-gaap") or {}
        if not gaap:
            return False
        company = (await self.repo.companies([cik])).get(cik)
        sic = company.sic if company is not None else None
        return sic is None or sic not in FINANCIAL_SIC

    async def _read_filings(
        self,
        client: EdgarClient,
        cik: int,
        companyfacts: Dict[str, Any],
        rows: Sequence[dict],
        read: Dict[str, int],
        summary: Dict[str, Any],
        extensions: bool = False,
        segments: bool = False,
    ) -> Tuple[int, Optional[EdgarRefused]]:
        """
        The filings behind this company's dividend and EPS gaps (`gaps`), newest first, at
        most `FILING_READS_PER_COMPANY` this pass. `read` is every stored read's `READER`: a
        filing is read once for what the reader looks for, so one read before EPS was
        looked for is read once more for an EPS gap, never for a dividend gap. A gap asks
        its `sources` in turn — its own filing, then the ones that repeat it — until one
        holds it; a gap the filings already read this pass cover (a comparative quarter, a
        year) is not asked for again. Returns how many were read, and the refusal that
        stopped them, if one did. A failure stamps nothing and stops this company's reads:
        next week asks again.

        With `extensions` (`_reads_extensions`), a fiscal year missing capex or debt
        (`extension_gaps`) is a gap too, asked of its own 10-K alone: every filing read
        then reads its label linkbase beside its instance for the company's own concepts.
        """
        by_accn = filings(companyfacts)
        # A filing companyfacts does not carry was read whole (`_read_skipped`); read again
        # for another gap, it is read whole again, or its figures would be lost.
        carried = companyfacts_accns(companyfacts)
        pending = gaps(rows)
        if extensions:
            pending += extension_gaps(rows, utc_today())
        if segments:
            pending += segment_gaps(rows, utc_today())
        pending.sort(key=lambda gap: gap[0], reverse=True)
        count = 0
        while pending and count < FILING_READS_PER_COMPANY:
            gap, metric = pending.pop(0)
            asked = sources(by_accn, gap)
            if metric.startswith(("extension:", "segments:")):
                asked = asked[:1]  # the period's own filing; its comparatives repeat it
            for filing in asked:
                if read.get(filing.accn, 0) >= READ_SINCE[metric]:
                    continue
                if count >= FILING_READS_PER_COMPANY:
                    break
                try:
                    found = await self._read_filing(
                        client, cik, filing, summary, whole=filing.accn not in carried,
                        extensions=extensions, segments=segments,
                    )
                except EdgarRefused as refused:
                    return count, refused
                except EdgarError as e:
                    summary["filing_errors"] += 1
                    logger.warning(f"filing {filing.accn} of CIK {cik} failed: {e}")
                    return count, None
                await self.repo.record_filing_read(
                    cik, filing.accn, filing.form, filing.filed, found, utcnow(), READER
                )
                await self.db.commit()
                read[filing.accn] = READER
                count += 1
                summary["filings_read"] += 1
                pending = [
                    (g, m) for g, m in pending if not covers(found, g, GAP_CONCEPTS[m])
                ]
                if covers(found, gap, GAP_CONCEPTS[metric]):
                    break
        return count, None

    async def _read_filing(
        self,
        client: EdgarClient,
        cik: int,
        filing: Filing,
        summary: Dict[str, Any],
        whole: bool = False,
        extensions: bool = False,
        segments: bool = False,
    ) -> List[dict]:
        """What one filing's instance holds; nothing when it has none, or none EDGAR lists.
        `whole`: its undimensioned chain facts too (`parse_instance`). `extensions`: the
        company's own concepts its label linkbase names like a metric too — one more
        request (`extension_metrics`); a linkbase that is not XML names none. `segments`: its
        segment columns' debt and the totals proving them (`parse_instance`)."""
        index = await client.filing_index(cik, filing.accn)
        name = instance_name(index) if index is not None else None
        if name is None:
            return []
        named = None
        linkbase = label_linkbase(index) if extensions else None
        if linkbase is not None:
            labels = await client.filing_document(cik, filing.accn, linkbase)
            try:
                named = extension_metrics(labels) if labels is not None else None
            except ET.ParseError as e:
                summary["filings_unreadable"] += 1
                logger.warning(f"filing {filing.accn} of CIK {cik}: {linkbase} ({e})")
        body = await client.filing_document(cik, filing.accn, name)
        if body is None:
            return []
        try:
            return parse_instance(body, whole=whole, extensions=named, segments=segments)
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
