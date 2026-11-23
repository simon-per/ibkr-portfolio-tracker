"""
The vendor fallback: a company no regulator the app reads covers gets its annual figures from
financialfilings.com (formerly financialreports.eu), owner's choice 2026-11-20
(docs/company-financials.md, *Vendor fallback*). Run by the weekly markets job after every
regulator's pass and by `app/cli/fetch_company_markets.py --source vendor`. **Needs
`FINANCIALREPORTS_API_KEY`** (the free tier: 500 credits a month); without it no request.

**Scope**: the companies the app names whose ISINs no page answers for — German (the
Unternehmensregister has no API), Swiss, Hong Kong, Australian, Canadian without an SEC filing
— heaviest first. A company a regulator later covers drops out (its regulator page wins, and
this pass no longer stamps it, `universe_seen_at`).

**Asked**: the company by ISIN (`/companies/?isin=`, 1 credit), then its fiscal years
(`/companies/{id}/financials/?fiscal_period=FY`, 40 credits, every year in one call), at most
once in `REFRESH_AFTER` a company, and only while the month's credits last
(`X-RateLimit-Monthly-Remaining` above `CREDIT_FLOOR`). A 401, 403 or 429 stops the pass.

**Read** (`facts_of`): each fiscal year's line items mapped by the vendor's own line code
(`LINES`, its `/line-item-definitions/` for the industrial, bank and insurer templates) to the
IFRS concept the one normaliser reads — standardised statements, not a filing's tags, so a code
`LINES` does not name is left out rather than guessed. Values are absolute decimals in the
statement's currency.

**Checked** (`sound_years`, the KPI audit of 2026-11-23): the vendor's extraction takes some
years from the wrong statements (Siemens' FY2018-19 from the parent's, Hydro One's FY2017, Allianz's
FY2011-12), leaves a split unrestated (Tencent's 5:1 of 2014) and misreads capex (Nestlé's, ABB's).
A stored answer is rewritten through the check without asking the vendor again (`READER`).
"""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.models.company_financials import VENDOR_SCHEME
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services.company_financials_normaliser import (
    NEIGHBOUR_RATIOS,
    NEIGHBOUR_TOLERANCE,
    REGULATOR_ANNUAL,
    _snap,
)
from app.services.company_markets import NamedCompany, named_companies, sec_isins
from app.services.edinet_sync import JAPAN_ISIN
from app.services.regulator_facts import normalise_reports, one_value_each

logger = logging.getLogger(__name__)

VENDOR_GATE = "vendor_financials"
SCHEDULED_SYNC_TYPE = "company_vendor_pages_refresh"
MANUAL_SYNC_TYPE = "manual_company_vendor_pages_fetch"
RESULT_KEY = "company_vendor_pages_result"
SOURCE = "financialfilings"
NOT_CONFIGURED = "FINANCIALREPORTS_API_KEY is not set; companies no regulator covers were not refreshed."
BASE = "https://api.financialreports.eu"
REFRESH_AFTER = timedelta(days=90)
#: Sooner once the next annual report should be out (a year and six weeks past the newest year
#: end), at most every `REPORT_RECHECK`: Siemens' and Infineon's FY2026, published within days
#: of the first read, would otherwise wait out the 90 days.
NEXT_REPORT_AFTER = timedelta(days=365 + 45)
REPORT_RECHECK = timedelta(days=30)
#: Stop asking below this many monthly credits: one company costs 41.
CREDIT_FLOOR = 45
FINANCIALS_COST = 41
REQUEST_INTERVAL_S = 1.0
READ = "read"
YEAR_DAYS = (350, 380)
#: What a stored answer has been through: 1 (NULL) nothing, 2 `sound_years`, 3 with a year
#: that has no share count on its neighbours' split basis.
READER = 3
#: A run of up to `DIP_MAX_RUN` years whose revenue is under this share of the years on both
#: sides is another entity's statements (Siemens' parent, 28bn and 22bn between 83bn and 57bn).
#: A company that shrinks for good (E.ON's 2015 spin-off) has no recovery after the run.
DIP_RATIO = 0.6
DIP_MAX_RUN = 2
#: Cash under this share of the next sound year's is a unit slip (Allianz's 1.48m and 141m
#: beside 17bn). Hydro One's real 30m before 757m is 4%.
CASH_SLIP_RATIO = 0.02
#: The vendor's capex is left out: Nestlé's FY2023 and FY2025 and ABB's FY2025 match no line of
#: their statements, so free cash flow is unknown on a vendor page.
UNPROVEN_CONCEPTS = frozenset(
    {"PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"}
)
SHARE_COUNTS = ("AdjustedWeightedAverageShares", "NumberOfSharesOutstanding")

MONEY, PER_SHARE, SHARES = "money", "per_share", "shares"
FLOW, INSTANT = "flow", "instant"

#: (IFRS concept, kind, timing, the vendor's line codes in order of preference), from its
#: `/line-item-definitions/` for the industrial, bank and insurer templates and Siemens' live
#: FY2025 checked against its annual report (2026-11-20). `net_income_loss` is the profit to
#: the parent's owners (Siemens: 9,620m beside 10,387m consolidated); a bank states its total
#: net revenue and its net interest income, an insurer its total revenues.
LINES: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    (
        "Revenue",
        MONEY,
        FLOW,
        ("revenue", "total_revenues", "total_net_revenue", "insurance_revenue"),
    ),
    ("InterestRevenueExpense", MONEY, FLOW, ("net_interest_income",)),
    ("CostOfSales", MONEY, FLOW, ("cost_of_revenue",)),
    ("GrossProfit", MONEY, FLOW, ("gross_profit",)),
    ("ProfitLossFromOperatingActivities", MONEY, FLOW, ("operating_income_loss",)),
    ("ProfitLossAttributableToOwnersOfParent", MONEY, FLOW, ("net_income_loss",)),
    ("ProfitLoss", MONEY, FLOW, ("consolidated_net_income",)),
    ("DilutedEarningsLossPerShare", PER_SHARE, FLOW, ("diluted_eps",)),
    (
        "AdjustedWeightedAverageShares",
        SHARES,
        FLOW,
        ("weighted_avg_diluted_shares_outstanding",),
    ),
    ("CashFlowsFromUsedInOperatingActivities", MONEY, FLOW, ("cash_from_operations",)),
    (
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        MONEY,
        FLOW,
        ("capital_expenditure_cf",),
    ),
    (
        "DividendsPaidClassifiedAsFinancingActivities",
        MONEY,
        FLOW,
        ("common_dividends_paid_cf", "total_dividends_paid_cf"),
    ),
    (
        "PaymentsToAcquireOrRedeemEntitysShares",
        MONEY,
        FLOW,
        ("repurchase_of_common_stock_cf",),
    ),
    (
        "CashAndCashEquivalents",
        MONEY,
        INSTANT,
        ("cash_and_equivalents", "cash_and_central_bank_balances"),
    ),
    ("EquityAttributableToOwnersOfParent", MONEY, INSTANT, ("total_common_equity",)),
    ("Equity", MONEY, INSTANT, ("total_equity",)),
    # The count on the filing date, kept at the period end: the market cap's count, which the
    # service checks against the diluted average (`MARKET_CAP_SHARE_TOLERANCE`).
    (
        "NumberOfSharesOutstanding",
        SHARES,
        INSTANT,
        ("shares_outstanding_on_filing_date",),
    ),
)

#: A diluted EPS further than this factor from the profit to owners over the diluted shares
#: is the vendor's extraction, not the company's (Serabi's FY2024 36.73: its cents as
#: dollars, 0.37 in its report; 2026-11-20): left out, since an unknown is absent.
EPS_CHECK_FACTOR = 2.0

#: The vendor states a cash outflow negative; the normaliser reads payments as the positive
#: US twins they map to (FCF = operating cash flow − capex).
PAYMENTS = frozenset(
    {
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "DividendsPaidClassifiedAsFinancingActivities",
        "PaymentsToAcquireOrRedeemEntitysShares",
    }
)


def is_configured() -> bool:
    return bool((settings.financialreports_api_key or "").strip())


def _number(text: Any) -> Optional[float]:
    try:
        return float(str(text))
    except (TypeError, ValueError):
        return None


def facts_of(body: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """
    The financials response's fiscal years as facts in the shape `regulator_facts` reads, and
    the vendor's line code per IFRS concept (`tagged`). Per period and concept the first of
    its codes the period states, from the period's own statements before a comparative; a
    diluted EPS its own profit and share count contradict is left out (`EPS_CHECK_FACTOR`).
    """
    facts: List[Dict[str, Any]] = []
    tagged: Dict[str, str] = {}
    default_currency = ((body.get("currency") or {}).get("code") or "").upper()
    for period in body.get("periods") or []:
        if (period.get("fiscal_period") or "").upper() != "FY":
            continue
        try:
            start = date.fromisoformat(str(period.get("period_start_date"))[:10])
            end = date.fromisoformat(str(period.get("period_end_date"))[:10])
        except ValueError:
            continue
        if not YEAR_DAYS[0] <= (end - start).days + 1 <= YEAR_DAYS[1]:
            continue
        lines: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        statements = sorted(
            period.get("statements") or [],
            key=lambda s: bool(s.get("is_comparative")),
        )
        for statement in statements:
            for item in statement.get("line_items") or []:
                lines.setdefault(str(item.get("code") or ""), (item, statement))
        found: Dict[str, Dict[str, Any]] = {}
        for concept, kind, timing, codes in LINES:
            for code in codes:
                item, statement = lines.get(code, (None, None))
                if item is None:
                    continue
                value = _number(item.get("value"))
                currency = (
                    item.get("currency")
                    or (statement.get("currency") or {}).get("code")
                    or default_currency
                    or ""
                ).upper()
                if value is None or (
                    kind != SHARES and not re.fullmatch(r"[A-Z]{3}", currency)
                ):
                    continue
                source = statement.get("source_filing") or {}
                found[concept] = (
                    {
                        "concept": concept,
                        "unit": "shares"
                        if kind == SHARES
                        else f"{currency}/shares"
                        if kind == PER_SHARE
                        else currency,
                        "start": None if timing == INSTANT else start.isoformat(),
                        "end": end.isoformat(),
                        "val": abs(value) if concept in PAYMENTS else value,
                        "accn": f"{SOURCE}:{source.get('id') or end.isoformat()}",
                        "filed": str(source.get("release_datetime") or "")[:10]
                        or end.isoformat(),
                        "form": REGULATOR_ANNUAL,
                    }
                )
                tagged.setdefault(concept, f"{SOURCE}:{code}")
                break
        if not _eps_agrees(found):
            found.pop("DilutedEarningsLossPerShare")
        facts.extend(found.values())
    return one_value_each(facts), tagged


def _eps_agrees(found: Dict[str, Dict[str, Any]]) -> bool:
    """False when the period's EPS, profit to owners and diluted shares all exist and the EPS
    is further than `EPS_CHECK_FACTOR` from the profit over the shares."""
    eps = found.get("DilutedEarningsLossPerShare")
    profit = found.get("ProfitLossAttributableToOwnersOfParent")
    shares = found.get("AdjustedWeightedAverageShares")
    if eps is None or profit is None or shares is None or not shares["val"]:
        return True
    implied = profit["val"] / shares["val"]
    if not implied or not eps["val"]:
        return True
    ratio = abs(eps["val"] / implied)
    return 1 / EPS_CHECK_FACTOR <= ratio <= EPS_CHECK_FACTOR


def _positive_by_end(facts: List[Dict[str, Any]], concept: str) -> Dict[str, float]:
    return {f["end"]: f["val"] for f in facts if f["concept"] == concept and f["val"] > 0}


def _unsound_ends(facts: List[Dict[str, Any]]) -> set:
    """The year ends whose revenue dips out of the series or whose cash is a unit slip."""
    revenue = _positive_by_end(facts, "Revenue")
    ends = sorted(revenue)
    bad = set()
    for i in range(1, len(ends) - 1):
        for run in range(1, DIP_MAX_RUN + 1):
            after = i + run
            if after >= len(ends):
                break
            edge = min(revenue[ends[i - 1]], revenue[ends[after]])
            if all(revenue[e] < DIP_RATIO * edge for e in ends[i:after]):
                bad.update(ends[i:after])
                break
    cash = _positive_by_end(facts, "CashAndCashEquivalents")
    newer = None
    for end in sorted(cash, reverse=True):
        if end in bad:
            continue
        if newer is not None and cash[end] < CASH_SLIP_RATIO * newer:
            bad.add(end)
            continue
        newer = cash[end]
    return bad


def _split_factors(facts: List[Dict[str, Any]]) -> Dict[str, float]:
    """Each year end's factor to the newest share basis: the steps of the diluted count that
    snap to a whole split of at least 2 (Tencent's 1,868m to 9,357m). A year with no count
    takes the factor of the nearest newer year that has one (Tencent's FY2009)."""
    counts = _positive_by_end(facts, "AdjustedWeightedAverageShares")
    factors: Dict[str, float] = {}
    factor = 1.0
    newer = None
    for end in sorted({f["end"] for f in facts}, reverse=True):
        if end in counts:
            if newer is not None:
                step = _snap(newer / counts[end], NEIGHBOUR_RATIOS, NEIGHBOUR_TOLERANCE)
                if step is not None and step.denominator == 1 and step >= 2:
                    factor *= float(step)
            newer = counts[end]
        factors[end] = factor
    return factors


def sound_years(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    A stored answer's facts less each unsound year, whole (`DIP_RATIO`, `CASH_SLIP_RATIO`) and
    the unproven lines (`UNPROVEN_CONCEPTS`), with the years before a split on the newest
    share basis: the vendor's years share no period, so the normaliser's vote cannot see it.
    """
    bad = _unsound_ends(facts)
    kept = [
        f for f in facts if f["end"] not in bad and f["concept"] not in UNPROVEN_CONCEPTS
    ]
    factors = _split_factors(kept)
    out = []
    for f in kept:
        factor = factors.get(f["end"], 1.0)
        if factor != 1.0 and f["concept"] == "DilutedEarningsLossPerShare":
            f = {**f, "val": f["val"] / factor}
        elif factor != 1.0 and f["concept"] in SHARE_COUNTS:
            f = {**f, "val": f["val"] * factor}
        out.append(f)
    return out


class VendorStop(Exception):
    """The vendor refused the key, the plan or the month's credits: the pass stops."""


class VendorFinancialsSync:
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
        self.requests = 0
        self.remaining: Optional[int] = None

    async def scope(self) -> List[NamedCompany]:
        """
        The named companies no other source's page answers for, heaviest first — and a
        Japanese one last: EDINET reads it free once its key exists, and a month's credits
        cover about ten companies (2026-11-20, sixteen in scope).
        """
        answered = await sec_isins(self.db)
        for company in await self.repo.other_companies():
            if company.scheme != VENDOR_SCHEME and company.isin and company.fetched_at:
                answered.add(company.isin.strip().upper())
        scope = [
            c
            for c in await named_companies(self.db)
            if c.isins and not any(i in answered for i in c.isins)
        ]
        return sorted(scope, key=lambda c: any(JAPAN_ISIN.match(i) for i in c.isins))

    async def refresh(
        self, *, force: bool = False, dry_run: bool = False
    ) -> Optional[Dict[str, Any]]:
        now = utcnow()
        scope = await self.scope()
        summary: Dict[str, Any] = {
            "status": "success",
            "configured": is_configured(),
            "in_scope": len(scope),
            "companies": 0,
            "asked": 0,
            "rows_written": 0,
            "not_found": 0,
            "errors": 0,
            "requests": 0,
            "credits_remaining": None,
            "outcomes": [],
        }
        if not is_configured():
            # Without its key the pass records nothing, as OpenDART's does.
            return None
        if dry_run:
            summary["message"] = (
                f"Vendor pages: {len(scope)} in scope (dry run, no request made)"
            )
            return summary
        warnings: List[str] = []
        # Every page already written stays listed, whatever this pass gets to ask.
        isins = {i for c in scope for i in c.isins}
        for company in await self.repo.other_companies(VENDOR_SCHEME):
            if company.fetched_at and (company.isin or "").strip().upper() in isins:
                company.universe_seen_at = now
        await self.db.commit()
        summary["rewritten"] = await self._rewrite_stored(now)
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(60.0),
            headers={
                "X-API-Key": settings.financialreports_api_key.strip(),
                "Accept": "application/json",
            },
        ) as client:
            for named in scope:
                if self.remaining is not None and self.remaining < CREDIT_FLOOR:
                    warnings.append(
                        "Vendor pages: the month's credits are spent; the rest "
                        "waits for next month."
                    )
                    break
                try:
                    outcome = await self._one(client, named, summary, now, force)
                except VendorStop as e:
                    warnings.append(f"Vendor pages: stopped ({e}).")
                    summary["outcomes"].append(
                        {"key": named.name, "outcome": "stopped"}
                    )
                    break
                except httpx.HTTPError as e:
                    summary["errors"] += 1
                    logger.warning("vendor read for %s failed: %s", named.name, e)
                    await self.db.rollback()
                    outcome = "error"
                summary["outcomes"].append({"key": named.name, "outcome": outcome})
        summary["requests"] = self.requests
        summary["credits_remaining"] = self.remaining
        summary["message"] = (
            f"Vendor pages: {summary['companies']} written of {len(scope)} in scope, "
            f"{summary['asked']} asked, {summary['rows_written']} rows, "
            f"{self.remaining if self.remaining is not None else '?'} credits left"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _due(self, company, now: datetime) -> bool:
        """Whether a page already asked is asked again: `REFRESH_AFTER` passed, or its next
        annual report should be out and it was not asked in `REPORT_RECHECK`."""
        asked = company.profile_checked_at
        if asked <= now - REFRESH_AFTER:
            return True
        if asked > now - REPORT_RECHECK:
            return False
        reports = await self.repo.company_reports(company.id)
        ends = [
            f["end"]
            for r in reports.values()
            if r.status == READ
            for f in r.facts or ()
        ]
        return bool(ends) and now.date() >= date.fromisoformat(max(ends)) + NEXT_REPORT_AFTER

    async def _rewrite_stored(self, now: datetime) -> int:
        """Each page's stored answer from before `READER`, through `sound_years`: no request,
        no credit."""
        rewritten = 0
        for company in await self.repo.other_companies(VENDOR_SCHEME):
            reports = await self.repo.company_reports(company.id)
            newest = max(
                (r for r in reports.values() if r.status == READ),
                key=lambda r: r.read_at,
                default=None,
            )
            if newest is None or (newest.reader or 1) >= READER:
                continue
            result = normalise_reports(
                company.name, sound_years(newest.facts or []), SOURCE, newest.tagged or {}
            )
            newest.reader = READER
            if result.rows:
                await self.repo.replace_company_rows(company, result.rows, now)
                rewritten += 1
            await self.db.commit()
        return rewritten

    async def _get(
        self, client: httpx.AsyncClient, path: str, **params: str
    ) -> Optional[Dict[str, Any]]:
        if self.requests:
            await self._sleep(REQUEST_INTERVAL_S)
        self.requests += 1
        response = await client.get(f"{BASE}{path}", params=params)
        left = response.headers.get("X-RateLimit-Monthly-Remaining")
        if left is not None and left.strip().lstrip("-").isdigit():
            self.remaining = int(left)
        if response.status_code in (401, 402, 403, 429):
            raise VendorStop(f"HTTP {response.status_code} for {path.split('?')[0]}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else None

    async def _one(
        self, client, named: NamedCompany, summary, now: datetime, force: bool
    ) -> str:
        existing = None
        for company in await self.repo.other_companies(VENDOR_SCHEME):
            if company.isin and company.isin.strip().upper() in named.isins:
                existing = company
                break
        if existing is not None:
            existing.universe_seen_at = (
                now if existing.fetched_at else existing.universe_seen_at
            )
            await self.db.commit()
            if (
                not force
                and existing.profile_checked_at
                and not await self._due(existing, now)
            ):
                return "unchanged"
        vendor_id = existing.native_id if existing is not None else None
        if vendor_id is None:
            found = None
            for isin in named.isins:
                body = await self._get(client, "/companies/", isin=isin)
                results = (body or {}).get("results") or []
                if (
                    results
                    and isinstance(results[0], dict)
                    and results[0].get("id") is not None
                ):
                    found = (str(results[0]["id"]), isin)
                    break
            if found is None:
                summary["not_found"] += 1
                return "not found"
            vendor_id, isin = found
            existing = await self.repo.ensure_other_company(
                VENDOR_SCHEME,
                vendor_id,
                isin=isin,
                country=isin[:2],
                ticker=named.ticker,
            )
        existing.name = named.name or existing.name
        existing.profile_checked_at = now
        await self.db.commit()
        if self.remaining is not None and self.remaining < FINANCIALS_COST:
            raise VendorStop("the month's credits are spent")
        body = await self._get(
            client, f"/companies/{vendor_id}/financials/", fiscal_period="FY"
        )
        summary["asked"] += 1
        facts, tagged = facts_of(body or {})
        if not facts:
            return "no figures"
        await self.repo.record_report(
            existing.id,
            f"financials-{now.date().isoformat()}",
            status=READ,
            read_at=now,
            accn=SOURCE,
            filed=now.date(),
            facts=facts,
            tagged=tagged,
            reader=READER,
        )
        result = normalise_reports(existing.name, sound_years(facts), SOURCE, tagged)
        if not result.rows:
            await self.db.commit()
            return "no figures"
        existing.universe_seen_at = now
        await self.repo.replace_company_rows(existing, result.rows, now)
        await self.db.commit()
        summary["companies"] += 1
        summary["rows_written"] += len(result.rows)
        return "written"
