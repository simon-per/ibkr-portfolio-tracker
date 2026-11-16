"""
The Company tab's last source: Alpha Vantage's statements, fill-only and proven
(docs/company-financials.md, *Alpha Vantage, last*; owner, 2026-11-16).

After every SEC layer — companyfacts, bridging, the filings companyfacts skipped, IFRS
instances, the company's own concepts and segment sums — a few recent quarters still
have no figure. Alpha Vantage's `INCOME_STATEMENT`, `BALANCE_SHEET` and `CASH_FLOW` may
hold them. The owner's rules, each enforced here:

- **Last and fill-only.** A figure SEC gave is never replaced, and a period is filled only
  where SEC reports the figure before and after it (`interior_gaps`): a figure the company
  stopped reporting (ABNB's capex from 2023, APP's from 2024) stays blank — a vendor's
  number there would be an estimate of a line the filing does not have. Recent quarters
  only (`RECENT_QUARTERS`), the ones the tab's TTM reads.
- **Proven per company and metric** (`_proven`), as `_bridge` proves a concept: on the
  periods SEC also reports, Alpha Vantage's value agrees within `BRIDGE_TOLERANCE` at least
  `BRIDGE_MIN_AGREEMENTS` times and on `BRIDGE_MIN_SHARE` of them; otherwise none of it is
  used for that metric. Its `capitalExpenditures` or `currentLongTermDebt` may be defined
  otherwise than the company's own line, and only agreement says it is not.
- **Absent is absent.** Its `"None"`, a `0` and a missing field are no figure. Only the
  currency SEC's rows are in; no bank, broker or insurer (`FINANCIAL_SIC`).
- **Its source on every row**: `alpha_vantage:<field>` (`alpha_vantage:capitalExpenditures`).
- **One budget** (`alpha_vantage`): the price fallback goes first; a company is asked at
  most once in `VENDOR_RECHECK_AFTER`, longer while asks fill nothing (the shared doubling,
  `miss_backoff`); a backlog spreads over days. Its own `sync_runs` row says what it asked,
  filled and spent — information, not a warning.

The fill is pure (`fill`) and is applied wherever a company's rows are written — after this
pass's asks, and after every EDGAR fetch (`CompanyFinancialsSync._one`) from the stored
statements (`vendor_statements`), so the weekly rewrite keeps what was proven.
"""

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.models.company_financials import SecCompany, SecurityCik
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services import alpha_vantage
from app.services.edgar_client import FINANCIAL_SIC
from app.services.company_financials_normaliser import (
    BRIDGE_MIN_AGREEMENTS,
    BRIDGE_MIN_SHARE,
    BRIDGE_TOLERANCE,
)
from app.services.miss_backoff import miss_backoff_until

logger = logging.getLogger(__name__)

SYNC_TYPE = "company_vendor_fill"
SOURCE = "alpha_vantage"
#: Per statement, the metric each Alpha Vantage field may fill. No diluted share count:
#: Alpha Vantage carries none (its `commonStockSharesOutstanding` is another figure).
FIELDS: Dict[str, Dict[str, str]] = {
    "INCOME_STATEMENT": {"revenue": "totalRevenue", "operating_income": "operatingIncome"},
    "CASH_FLOW": {"operating_cash_flow": "operatingCashflow", "capex": "capitalExpenditures"},
    "BALANCE_SHEET": {
        "cash": "cashAndCashEquivalentsAtCarryingValue",
        "debt_current": "currentLongTermDebt",
        "debt_noncurrent": "longTermDebtNoncurrent",
    },
}
#: The quarters a gap is looked for in: the newest eight, what the tab's TTM points read.
RECENT_QUARTERS = 8
SAME_PERIOD_DAYS = 7
#: A company is asked at most this often, and longer while its asks fill nothing.
VENDOR_RECHECK_AFTER = timedelta(days=7)


def _number(raw: Any) -> Optional[float]:
    """Alpha Vantage's figure, or None: its `"None"`, a zero and a missing field are no
    figure."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value and value == value and abs(value) != float("inf") else None


def compact(function: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """What is kept of one statement: per report, its date, currency and the mapped
    fields — not the whole answer."""
    fields = set(FIELDS[function].values())
    out: Dict[str, Any] = {}
    for grain, key in (("quarterly", "quarterlyReports"), ("annual", "annualReports")):
        out[grain] = [
            {
                "end": r.get("fiscalDateEnding"),
                "currency": r.get("reportedCurrency"),
                **{f: r.get(f) for f in fields if f in r},
            }
            for r in body.get(key) or ()
            if isinstance(r, dict) and isinstance(r.get("fiscalDateEnding"), str)
        ]
    return out


def _currency(rows: Sequence[Dict[str, Any]]) -> Optional[str]:
    units = Counter(
        r.get("unit") for r in rows if r["metric"] in ("revenue", "net_income") and r.get("unit")
    )
    return units.most_common(1)[0][0] if units else None


def _quarter_ends(rows: Sequence[Dict[str, Any]]) -> List[date]:
    return sorted({r["period_end"] for r in rows if r["fiscal_period"] != "FY"})


def interior_gaps(
    rows: Sequence[Dict[str, Any]], filled: Sequence[Dict[str, Any]] = ()
) -> Dict[str, List[date]]:
    """
    Per metric this fill knows, the recent quarter ends (`RECENT_QUARTERS`) where SEC's
    rows lack it while reporting it in an earlier and a later quarter: a hole, not a line
    the company stopped (or never started) reporting. Debt counts a half missing. A gap
    one of `filled` (Alpha Vantage's rows already written) covers is not one to ask about.
    """
    ends = _quarter_ends(rows)
    recent = ends[-RECENT_QUARTERS:]
    have: Dict[str, set] = defaultdict(set)
    for r in rows:
        if r["fiscal_period"] != "FY" and not str(r.get("source_concept", "")).startswith(
            f"{SOURCE}:"
        ):
            have[r["metric"]].add(r["period_end"])
    covered = {(r["metric"], r["period_end"]) for r in filled}
    out: Dict[str, List[date]] = {}
    for mapping in FIELDS.values():
        for metric in mapping:
            got = have.get(metric, set())
            if not got:
                continue
            first, last = min(got), max(got)
            holes = [
                e for e in recent
                if e not in got and first < e < last and (metric, e) not in covered
            ]
            if holes:
                out[metric] = holes
    return out


def _series(doc: Dict[str, Any], field: str, currency: Optional[str]) -> Dict[Tuple[str, date], float]:
    out: Dict[Tuple[str, date], float] = {}
    for grain, reports in (("Q", doc.get("quarterly") or ()), ("FY", doc.get("annual") or ())):
        for r in reports:
            if currency is not None and r.get("currency") != currency:
                continue
            try:
                end = date.fromisoformat(r["end"])
            except (TypeError, ValueError):
                continue
            value = _number(r.get(field))
            if value is not None:
                out[(grain, end)] = value
    return out


def _match(end: date, ends: Sequence[date]) -> Optional[date]:
    near = [e for e in ends if abs((e - end).days) <= SAME_PERIOD_DAYS]
    return min(near, key=lambda e: abs((e - end).days)) if near else None


def _proven(own: Dict[Tuple[str, date], float], theirs: Dict[Tuple[str, date], float]) -> bool:
    shared = [k for k in theirs if own.get(k)]
    agree = [k for k in shared if abs(theirs[k] / own[k] - 1) <= BRIDGE_TOLERANCE]
    return len(agree) >= BRIDGE_MIN_AGREEMENTS and len(agree) >= BRIDGE_MIN_SHARE * len(shared)


def fill(
    rows: Sequence[Dict[str, Any]], docs: Dict[str, Dict[str, Any]], fetched: date
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, int]]]:
    """
    The rows Alpha Vantage's stored statements (`docs`, by function, as `compact` keeps
    them, with the date it answered as `fetched`, else `fetched` here) add to SEC's `rows`: each an interior gap of a recent quarter (`interior_gaps`),
    for a metric whose Alpha Vantage series agrees with SEC's where both report
    (`_proven`), in SEC's currency. Pure. Returns the new rows (SEC's untouched, none of a
    previous fill's) and what filled what: {metric: {"alpha_vantage:<field>": n}}.
    """
    sec = [r for r in rows if not str(r.get("source_concept", "")).startswith(f"{SOURCE}:")]
    gaps = interior_gaps(sec)
    currency = _currency(sec)
    if not gaps or currency is None:
        return list(sec), {}
    labels: Dict[Tuple[str, date], Tuple[int, str]] = {}
    for r in sec:
        grain = "FY" if r["fiscal_period"] == "FY" else "Q"
        labels[(grain, r["period_end"])] = (r["fiscal_year"], r["fiscal_period"])
    ends = {g: sorted(e for gg, e in labels if gg == g) for g in ("Q", "FY")}
    added: List[Dict[str, Any]] = []
    filled: Dict[str, Dict[str, int]] = {}
    for function, mapping in FIELDS.items():
        doc = docs.get(function)
        if not doc:
            continue
        for metric, field in mapping.items():
            if metric not in gaps:
                continue
            theirs: Dict[Tuple[str, date], float] = {}
            for (grain, end), value in _series(doc, field, currency).items():
                matched = _match(end, ends[grain])
                if matched is not None:
                    theirs[(grain, matched)] = value
            own = {
                ("FY" if r["fiscal_period"] == "FY" else "Q", r["period_end"]): r["value"]
                for r in sec
                if r["metric"] == metric
            }
            if not _proven(own, theirs):
                continue
            for end in gaps[metric]:
                value = theirs.get(("Q", end))
                if value is None:
                    continue
                fiscal_year, fiscal_period = labels[("Q", end)]
                source = f"{SOURCE}:{field}"
                try:
                    answered = date.fromisoformat(doc.get("fetched") or "")
                except ValueError:
                    answered = fetched
                added.append(
                    {
                        "period_end": end,
                        "fiscal_year": fiscal_year,
                        "fiscal_period": fiscal_period,
                        "metric": metric,
                        "value": value,
                        "unit": currency,
                        "source_concept": source,
                        "filed": answered,
                    }
                )
                filled.setdefault(metric, {}).setdefault(source, 0)
                filled[metric][source] += 1
    out = list(sec) + added
    out.sort(key=lambda r: (r["period_end"], r["fiscal_period"], r["metric"]))
    return out, filled


def needed(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """The statements whose fields could fill this company's gaps, in `FIELDS` order — the
    gaps its rows still have once Alpha Vantage's rows already written count."""
    vendor = [r for r in rows if str(r.get("source_concept", "")).startswith(f"{SOURCE}:")]
    gaps = interior_gaps(rows, vendor)
    return [f for f, mapping in FIELDS.items() if any(m in gaps for m in mapping)]


def _row_dict(row) -> Dict[str, Any]:
    return {
        "period_end": row.period_end,
        "fiscal_year": row.fiscal_year,
        "fiscal_period": row.fiscal_period,
        "metric": row.metric,
        "value": row.value,
        "unit": row.unit,
        "source_concept": row.source_concept,
        "filed": row.filed,
    }


def vendor_backoff_until(company: Optional[SecCompany]) -> Optional[datetime]:
    """When Alpha Vantage may next be asked about this company, or None if it may now: a
    week after the last ask, longer while asks fill nothing (the shared doubling)."""
    if company is None or company.vendor_checked_at is None:
        return None
    weekly = company.vendor_checked_at + VENDOR_RECHECK_AFTER
    doubling = miss_backoff_until(company.vendor_checked_at, company.vendor_miss_count)
    return max(weekly, doubling) if doubling is not None else weekly


class CompanyVendorFill:
    """The daily pass. `sleep` is handed to `alpha_vantage.ask` (tests: no waiting)."""

    def __init__(self, db: AsyncSession, sleep=None):
        self.db = db
        self.repo = CompanyFinancialsRepository(db)
        self._sleep = sleep

    async def _tickers(self) -> Dict[int, str]:
        """CIK -> the ticker Alpha Vantage is asked by: a held listing's SEC spelling
        (the lowest), else the constituent ticker the pass stored."""
        out: Dict[int, str] = {}
        rows = (await self.db.execute(select(SecurityCik.cik, SecurityCik.sec_ticker))).all()
        for cik, ticker in sorted(rows, key=lambda r: (r[1] or "")):
            if cik is not None and ticker:
                out.setdefault(cik, ticker)
        for company in (await self.repo.companies()).values():
            if company.ticker:
                out.setdefault(company.cik, company.ticker)
        return out

    async def run(self) -> Dict[str, Any]:
        """
        Ask Alpha Vantage about the companies whose recent quarters still have interior
        gaps it could fill, stalest ask first, while today's statement budget lasts; store
        what it answered and apply the fill. Returns a summary for a `sync_runs` row.
        """
        if not alpha_vantage.is_configured():
            return {
                "status": "skipped",
                "message": "Company statements: skipped, ALPHA_VANTAGE_API_KEY is not set",
            }
        now, today = utcnow(), utc_today()
        companies = await self.repo.companies()
        tickers = await self._tickers()
        coverage = await self.repo.coverage(list(companies))
        summary: Dict[str, Any] = {
            "status": "success",
            "due": 0,
            "asked": [],
            "filled": [],
            "unproven": [],
            "spent": 0,
            "capped": False,
        }
        due: List[Tuple[Any, int, List[str], List[Dict[str, Any]]]] = []
        for cik, company in companies.items():
            if cik not in coverage or not tickers.get(cik):
                continue
            if company.sic is not None and company.sic in FINANCIAL_SIC:
                continue
            wait = vendor_backoff_until(company)
            if wait is not None and wait > now:
                continue
            rows = [_row_dict(r) for r in await self.repo.rows_for(cik)]
            functions = needed(rows)
            if functions:
                due.append((company.vendor_checked_at or datetime.min, cik, functions, rows))
        due.sort(key=lambda d: (d[0], d[1]))
        summary["due"] = len(due)
        before = (await alpha_vantage.budget(self.db, alpha_vantage.STATEMENTS))["spent"]
        try:
            for _, cik, functions, rows in due:
                state = await alpha_vantage.budget(self.db, alpha_vantage.STATEMENTS)
                if state["left"] < len(functions):
                    break  # the rest waits for another day's budget
                label = tickers[cik]
                docs = await self.repo.vendor_statements(cik)
                for function in functions:
                    summary["asked"].append(f"{label} {function}")
                    body = await alpha_vantage.ask(
                        self.db, function, alpha_vantage.STATEMENTS, sleep=self._sleep,
                        symbol=label.replace("-", "."),  # SEC's BRK-B is its BRK.B
                    )
                    if body is not None:
                        docs[function] = {**compact(function, body), "fetched": today.isoformat()}
                        await self.repo.write_vendor_statement(cik, function, docs[function], now)
                new_rows, filled = fill(rows, docs, today)
                await self.repo.replace_vendor_rows(
                    cik, [r for r in new_rows if r["source_concept"].startswith(f"{SOURCE}:")],
                    now,
                )
                await self.repo.record_vendor_ask(cik, now, filled=bool(filled))
                await self.db.commit()
                for metric, by_source in sorted(filled.items()):
                    for source, n in sorted(by_source.items()):
                        summary["filled"].append(f"{label} {metric} <- {source} ({n})")
                gaps = interior_gaps(new_rows, [
                    r for r in new_rows if r["source_concept"].startswith(f"{SOURCE}:")
                ])
                left = sorted(gaps)
                if left:
                    summary["unproven"].append(f"{label} ({', '.join(left)})")
        except alpha_vantage.AlphaVantageCapped as e:
            summary["capped"] = True
            logger.info(f"Company statements: {e}")
            await self.db.commit()
        summary["budget"] = await alpha_vantage.budget(self.db, alpha_vantage.STATEMENTS)
        # What this pass spent: the day's count moved by it (a refused call is not spent).
        summary["spent"] = max(summary["budget"]["spent"] - before, 0)
        summary["message"] = (
            f"Company statements: {len(summary['filled'])} fills from {summary['spent']} "
            f"Alpha Vantage calls, {summary['due']} companies with gaps it could fill"
        )
        return summary
