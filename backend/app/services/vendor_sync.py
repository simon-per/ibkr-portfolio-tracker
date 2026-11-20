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

**Read** (`facts_of`): each fiscal year's line items mapped by their code or name
(`LINES`) to the IFRS concept the one normaliser reads — the vendor's own standardised
statements, not a filing's tags, so a line no pattern names is left out rather than guessed.
Values are absolute decimals in the statement's currency.
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
from app.services.company_financials_normaliser import REGULATOR_ANNUAL
from app.services.company_markets import NamedCompany, named_companies, sec_isins
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
#: Stop asking below this many monthly credits: one company costs 41.
CREDIT_FLOOR = 45
FINANCIALS_COST = 41
REQUEST_INTERVAL_S = 1.0
READ = "read"
YEAR_DAYS = (350, 380)

#: (IFRS concept, kind, pattern over "<code> <name>" lower-cased), first match wins per
#: period. Attributable profit before the consolidated one; an instant for balances.
LINES: Tuple[Tuple[str, str, str, "re.Pattern[str]"], ...] = tuple(
    (concept, kind, timing, re.compile(pattern))
    for concept, kind, timing, pattern in (
        (
            "Revenue",
            "money",
            "flow",
            r"^(revenue|total_revenues?|revenues) |^\S+ total revenues?$",
        ),
        (
            "ProfitLossAttributableToOwnersOfParent",
            "money",
            "flow",
            r"net income.*attributable to (the )?(parent|owners|shareholders|common|equity holders)",
        ),
        (
            "ProfitLoss",
            "money",
            "flow",
            r"^net_income_loss |^\S+ net income( \(loss\))?$",
        ),
        (
            "DilutedEarningsLossPerShare",
            "per_share",
            "flow",
            r"diluted.*(eps|earnings per share)|(eps|earnings per share).*diluted",
        ),
        (
            "ProfitLossFromOperatingActivities",
            "money",
            "flow",
            r"^(operating_income|operating_profit|ebit) |^\S+ operating (income|profit)( \(loss\))?$",
        ),
        (
            "CashFlowsFromUsedInOperatingActivities",
            "money",
            "flow",
            r"cash (flows? )?(from|provided by|generated (by|from)|used in) operating",
        ),
        (
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "money",
            "flow",
            r"capital expenditure|capex|purchases? of property",
        ),
        (
            "CashAndCashEquivalents",
            "money",
            "instant",
            r"^\S+ cash and (cash )?equivalents$",
        ),
        (
            "EquityAttributableToOwnersOfParent",
            "money",
            "instant",
            r"(equity attributable to (the )?(owners|shareholders|equity holders) of the parent)"
            r"|total (shareholders'?|stockholders'?) equity",
        ),
    )
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
    the vendor's line code per IFRS concept (`tagged`). Per period and concept the first line
    a pattern names; a capex line is a payment, written as the outflow IFRS tags (negative).
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
        taken: set = set()
        for statement in period.get("statements") or []:
            source = statement.get("source_filing") or {}
            filed = str(source.get("release_datetime") or "")[:10] or end.isoformat()
            accn = f"{SOURCE}:{source.get('id') or end.isoformat()}"
            for item in statement.get("line_items") or []:
                text = (
                    f"{item.get('code') or ''} {item.get('name') or ''}".strip().lower()
                )
                value = _number(item.get("value"))
                currency = (item.get("currency") or default_currency or "").upper()
                if value is None or not re.fullmatch(r"[A-Z]{3}", currency):
                    continue
                for concept, kind, timing, pattern in LINES:
                    if concept in taken or not pattern.search(text):
                        continue
                    if concept.startswith("PurchaseOf"):
                        value = -abs(value)
                    facts.append(
                        {
                            "concept": concept,
                            "unit": f"{currency}/shares"
                            if kind == "per_share"
                            else currency,
                            "start": None if timing == "instant" else start.isoformat(),
                            "end": end.isoformat(),
                            "val": value,
                            "accn": accn,
                            "filed": filed,
                            "form": REGULATOR_ANNUAL,
                        }
                    )
                    tagged.setdefault(concept, f"{SOURCE}:{item.get('code')}")
                    taken.add(concept)
                    break
    return one_value_each(facts), tagged


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
        """The named companies no other source's page answers for, heaviest first."""
        answered = await sec_isins(self.db)
        for company in await self.repo.other_companies():
            if company.scheme != VENDOR_SCHEME and company.isin and company.fetched_at:
                answered.add(company.isin.strip().upper())
        return [
            c
            for c in await named_companies(self.db)
            if c.isins and not any(i in answered for i in c.isins)
        ]

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
                and existing.profile_checked_at > now - REFRESH_AFTER
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
        )
        result = normalise_reports(existing.name, facts, SOURCE, tagged)
        if not result.rows:
            await self.db.commit()
            return "no figures"
        existing.universe_seen_at = now
        await self.repo.replace_company_rows(existing, result.rows, now)
        await self.db.commit()
        summary["companies"] += 1
        summary["rows_written"] += len(result.rows)
        return "written"
