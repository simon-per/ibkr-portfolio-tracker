"""
Reads and writes of `company_financials`, `sec_companies`, `security_ciks`,
`sec_filing_reads` and the company prices (docs/company-financials.md).

`replace_rows` is the only write of a company's figures and it **refuses whole**: the
company's rows are deleted and the new set inserted in one transaction, so a reader never
sees half of one fetch beside half of the previous one. `write_prices` does the same for
the stretch of closes one fetch covers.

A company is an SEC filer (`SecCompany`, rows by `cik`) or one of `companies` (`Company`,
rows by `company_id`, docs/company-financials.md *Markets beyond the SEC*); the readers that
take a company object (`rows_of`, `coverage_of`) serve both through `_owned_by`.
"""

from datetime import date, datetime
import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_financials import (
    Company,
    CompanyFinancial,
    CompanyReport,
    DartCorpCode,
    CompanyPrice,
    CompanySplit,
    CompanyYahooProfile,
    SecCompany,
    SecFilingRead,
    SecNameMatch,
    SecTicker,
    SecurityCik,
    VendorStatement,
)
from app.services.edgar_client import FOREIGN_ANNUAL_PREFIXES
from app.services.miss_backoff import miss_backoff_until


def _owned_by(company):
    """The `company_financials` rows of this company, an SEC filer or one of `companies`."""
    if company.cik is not None:
        return CompanyFinancial.cik == company.cik
    return CompanyFinancial.company_id == company.company_id


def facts_backoff_until(row) -> Optional[datetime]:
    """When companyfacts may next be asked for this company, or None if it may now."""
    if row is None:
        return None
    return miss_backoff_until(row.facts_checked_at, row.facts_miss_count)


def yahoo_profile_backoff_until(
    row: Optional[CompanyYahooProfile],
) -> Optional[datetime]:
    """When the Company tab's Yahoo chunk may next ask about a company, or None."""
    if row is None:
        return None
    return miss_backoff_until(row.checked_at, row.miss_count)


#: The metrics whose unit names a company's reporting currency, in order of preference.
CURRENCY_METRICS = ("revenue", "net_income")


#: Every figure column of `company_yahoo_profiles`, replaced as one set on each answer.
YAHOO_PROFILE_COLUMNS = tuple(
    c.name
    for c in CompanyYahooProfile.__table__.columns
    if c.name not in ("cik", "fetched_at", "checked_at", "miss_count")
)


def prices_backoff_until(row: Optional[SecCompany]) -> Optional[datetime]:
    """When Yahoo may next be asked for this company's prices, or None if it may now."""
    if row is None:
        return None
    return miss_backoff_until(row.prices_checked_at, row.prices_miss_count)


class CompanyFinancialsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── security_ciks ────────────────────────────────────────────────────────

    async def cik_rows(self, security_ids: Iterable[int]) -> Dict[int, SecurityCik]:
        ids = list(security_ids)
        if not ids:
            return {}
        result = await self.session.execute(
            select(SecurityCik).where(SecurityCik.security_id.in_(ids))
        )
        return {row.security_id: row for row in result.scalars()}

    async def record_cik(
        self, security_id: int, cik: Optional[int], ticker: str, checked_at: datetime
    ) -> SecurityCik:
        row = await self.session.get(SecurityCik, security_id)
        if row is None:
            row = SecurityCik(security_id=security_id, cik_checked_at=checked_at)
            self.session.add(row)
        row.cik, row.sec_ticker, row.cik_checked_at = cik, ticker, checked_at
        return row

    async def listings(self, ciks: Iterable[int]) -> Dict[int, List[int]]:
        """CIK -> the security ids mapped to it, for these CIKs."""
        wanted = list(ciks)
        if not wanted:
            return {}
        result = await self.session.execute(
            select(SecurityCik.cik, SecurityCik.security_id).where(
                SecurityCik.cik.in_(wanted)
            )
        )
        out: Dict[int, List[int]] = {}
        for cik, sid in result.all():
            out.setdefault(cik, []).append(sid)
        return out

    async def all_cik_rows(self) -> List[SecurityCik]:
        return list((await self.session.execute(select(SecurityCik))).scalars())

    # ── sec_tickers ──────────────────────────────────────────────────────────

    async def ticker_rows(self) -> Dict[str, SecTicker]:
        result = await self.session.execute(select(SecTicker))
        return {row.ticker: row for row in result.scalars()}

    async def record_ticker(
        self,
        ticker: str,
        cik: Optional[int],
        checked_at: datetime,
        title: Optional[str] = None,
    ) -> None:
        """The SEC's answer for a ticker: its CIK (None: not listed) and its name. A new
        CIK forgets the annual form recorded for the old one."""
        row = await self.session.get(SecTicker, ticker)
        if row is None:
            row = SecTicker(ticker=ticker, checked_at=checked_at)
            self.session.add(row)
        if row.cik != cik:
            row.annual_form = None
        row.cik, row.checked_at, row.title = cik, checked_at, title

    async def record_annual_form(
        self,
        ticker: str,
        form: str,
        period_end: Optional[date] = None,
        checked_at: Optional[datetime] = None,
    ) -> None:
        """The form of the newest annual report the ticker's filer lists (`-`: none), the
        period it ends, and when the list was asked."""
        row = await self.session.get(SecTicker, ticker)
        if row is not None:
            row.annual_form = form
            row.annual_period_end = period_end
            row.form_checked_at = checked_at

    # ── sec_name_matches ─────────────────────────────────────────────────────

    async def name_matches(self) -> Dict[str, SecNameMatch]:
        """ISIN -> the SEC filer found by the company's name (`SecNameMatch`)."""
        result = await self.session.execute(select(SecNameMatch))
        return {row.isin: row for row in result.scalars()}

    async def record_name_match(
        self,
        isin: str,
        cik: Optional[int],
        ticker: Optional[str],
        title: Optional[str],
        checked_at: datetime,
    ) -> None:
        row = await self.session.get(SecNameMatch, isin)
        if row is None:
            row = SecNameMatch(isin=isin, checked_at=checked_at)
            self.session.add(row)
        row.cik, row.ticker, row.title, row.checked_at = cik, ticker, title, checked_at

    async def isins_paged_elsewhere(self) -> set:
        """The ISINs of another regulator's companies with stored figures (an ESEF or
        OpenDART page): a 20-F filer is not taken over them (`company_financials_sync`)."""
        result = await self.session.execute(
            select(Company.isin)
            .join(CompanyFinancial, CompanyFinancial.company_id == Company.id)
            .where(Company.isin.is_not(None))
            .distinct()
        )
        return {(i or "").strip().upper() for i in result.scalars()} - {""}

    async def ordinary_priced_ciks(self) -> set:
        """
        The SEC filers priced by their ordinary share, never at Yahoo (docs/company-
        financials.md, *Prices without Yahoo*): a 20-F filer kept under a non-US ISIN — the
        companies found by their name (SAP, Novartis, Toyota). Its New York listing, where it
        has one, is a depositary receipt that can stand for ten shares (Toyota's) or two
        (BHP's) beside an EPS per ordinary share, and nothing in companyfacts says which.
        """
        twenty_f = {
            cik
            for cik, form in (
                await self.session.execute(
                    select(SecTicker.cik, SecTicker.annual_form).where(
                        SecTicker.cik.is_not(None), SecTicker.annual_form.is_not(None)
                    )
                )
            )
            if form.startswith(FOREIGN_ANNUAL_PREFIXES)
        }
        if not twenty_f:
            return set()
        result = await self.session.execute(
            select(SecCompany.cik, SecCompany.isin).where(SecCompany.cik.in_(sorted(twenty_f)))
        )
        return {
            cik
            for cik, isin in result
            if isin and not isin.strip().upper().startswith("US")
        }

    # ── sec_companies ────────────────────────────────────────────────────────

    async def companies(
        self, ciks: Optional[Iterable[int]] = None
    ) -> Dict[int, SecCompany]:
        """CIK -> company, for these CIKs or every stored one."""
        query = select(SecCompany)
        if ciks is not None:
            wanted = list(ciks)
            if not wanted:
                return {}
            query = query.where(SecCompany.cik.in_(wanted))
        return {row.cik: row for row in (await self.session.execute(query)).scalars()}

    async def ensure_company(
        self,
        cik: int,
        ticker: Optional[str] = None,
        name: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> SecCompany:
        """The company's row, created if new; what is given replaces what was stored."""
        row = await self.session.get(SecCompany, cik)
        if row is None:
            row = SecCompany(cik=cik)
            self.session.add(row)
        if ticker:
            row.ticker = ticker
        if name:
            row.name = name
        if isin:
            row.isin = isin
        return row

    async def latest_universe_stamp(self) -> Optional[datetime]:
        return (
            await self.session.execute(select(func.max(SecCompany.universe_seen_at)))
        ).scalar()

    # ── companies (the other regulators) ─────────────────────────────────────

    async def other_companies(self, scheme: Optional[str] = None) -> List[Company]:
        """Every company of another regulator than the SEC, or of one scheme."""
        query = select(Company).order_by(Company.id)
        if scheme is not None:
            query = query.where(Company.scheme == scheme)
        return list((await self.session.execute(query)).scalars())

    async def other_company(self, scheme: str, native_id: str) -> Optional[Company]:
        return (
            await self.session.execute(
                select(Company).where(
                    Company.scheme == scheme, Company.native_id == native_id
                )
            )
        ).scalar_one_or_none()

    async def ensure_other_company(
        self, scheme: str, native_id: str, **fields: Optional[str]
    ) -> Company:
        """The company's row, created if new; what is given (not None) replaces what was
        stored."""
        row = await self.other_company(scheme, native_id)
        if row is None:
            row = Company(scheme=scheme, native_id=native_id)
            self.session.add(row)
            await self.session.flush()
        for name, value in fields.items():
            if value is not None:
                setattr(row, name, value)
        return row

    async def latest_other_universe_stamp(self, scheme: str) -> Optional[datetime]:
        return (
            await self.session.execute(
                select(func.max(Company.universe_seen_at)).where(Company.scheme == scheme)
            )
        ).scalar()

    # ── company_reports, dart_corp_codes ─────────────────────────────────────

    async def company_reports(self, company_id: int) -> Dict[str, CompanyReport]:
        """Every report of the company asked about, by its report id."""
        result = await self.session.execute(
            select(CompanyReport).where(CompanyReport.company_id == company_id)
        )
        return {row.report_id: row for row in result.scalars()}

    async def record_report(
        self,
        company_id: int,
        report_id: str,
        *,
        status: str,
        read_at: datetime,
        period_end: Optional[date] = None,
        accn: Optional[str] = None,
        filed: Optional[date] = None,
        facts: Sequence[dict] = (),
        tagged: Optional[Dict[str, str]] = None,
        non_standard: int = 0,
    ) -> None:
        """What one report held (`read`), or that it held nothing (`none`)."""
        await self.session.merge(
            CompanyReport(
                company_id=company_id,
                report_id=report_id,
                period_end=period_end,
                accn=accn,
                filed=filed,
                status=status,
                facts=list(facts),
                tagged=dict(tagged or {}),
                non_standard=non_standard,
                read_at=read_at,
            )
        )

    async def dart_codes(self) -> Dict[str, DartCorpCode]:
        """ISIN -> OpenDART's answer for the listing."""
        result = await self.session.execute(select(DartCorpCode))
        return {row.isin: row for row in result.scalars()}

    async def record_dart_code(
        self,
        isin: str,
        stock_code: str,
        corp_code: Optional[str],
        checked_at: datetime,
    ) -> None:
        row = await self.session.get(DartCorpCode, isin)
        if row is None:
            row = DartCorpCode(isin=isin, stock_code=stock_code, checked_at=checked_at)
            self.session.add(row)
        row.stock_code, row.corp_code, row.checked_at = stock_code, corp_code, checked_at

    async def record_other_miss(self, company: Company, checked_at: datetime) -> None:
        """Another regulator had nothing this reads for the company: the shared back-off."""
        company.facts_checked_at = checked_at
        company.facts_miss_count = (company.facts_miss_count or 0) + 1

    async def replace_company_rows(
        self, company: Company, rows: Sequence[dict], fetched_at: datetime
    ) -> None:
        """Every figure of one company of another regulator, replaced as one set — the
        same write as `replace_rows`, keyed by `company_id`; clears its miss stamps."""
        await self._replace(company, rows, fetched_at)
        company.fetched_at = fetched_at
        company.facts_checked_at = None
        company.facts_miss_count = None

    async def _replace(self, company, rows: Sequence[dict], fetched_at: datetime) -> None:
        """Delete the company's rows and insert `rows`: the one figure write."""
        await self.session.execute(delete(CompanyFinancial).where(_owned_by(company)))
        owner = (
            {"cik": company.cik}
            if company.cik is not None
            else {"company_id": company.company_id}
        )
        self.session.add_all(
            CompanyFinancial(**owner, fetched_at=fetched_at, **row) for row in rows
        )

    async def record_miss(self, cik: int, checked_at: datetime) -> None:
        """companyfacts had nothing this app reads; `facts_backoff_until` spaces the next ask."""
        row = await self.ensure_company(cik)
        row.facts_checked_at = checked_at
        row.facts_miss_count = (row.facts_miss_count or 0) + 1

    async def record_lag(
        self, cik: int, checked_at: datetime, misses: Optional[int]
    ) -> None:
        """
        A fetch that stopped short of the filing the SEC lists (companyfacts lags it):
        spaced like a miss (`facts_backoff_until`), counting on from `misses` — the
        company's count before the fetch, which the write cleared (review item CO5).
        """
        row = await self.ensure_company(cik)
        row.facts_checked_at = checked_at
        row.facts_miss_count = (misses or 0) + 1

    async def record_profile(
        self, cik: int, profile: Optional[dict], checked_at: datetime
    ) -> None:
        """
        The submissions document's answer (`EdgarClient.submissions`), replacing what was
        stored — a 404 (`None`) clears it: the SEC states nothing now. Stamped either way.
        """
        row = await self.ensure_company(cik)
        profile = profile or {}
        row.sic = profile.get("sic")
        row.sic_description = profile.get("sic_description")
        row.fiscal_year_end = profile.get("fiscal_year_end")
        row.owner_org = profile.get("owner_org")
        row.submissions_checked_at = checked_at

    async def record_vendor_ask(self, cik: int, checked_at: datetime, filled: bool) -> None:
        """Alpha Vantage was asked (`company_vendor_fill`): stamped, and a miss counted
        while it fills nothing, cleared once it does."""
        row = await self.ensure_company(cik)
        row.vendor_checked_at = checked_at
        row.vendor_miss_count = None if filled else (row.vendor_miss_count or 0) + 1

    async def record_statement_model(self, cik: int, model: Optional[str]) -> None:
        """What the newest annual report's concepts show (`statement_model`), with the
        figures it came from."""
        row = await self.ensure_company(cik)
        row.statement_model = model

    async def record_listing(self, cik: int, checked_at: datetime) -> None:
        """The filing list was asked for a skipped filing (`filings_listed_at`)."""
        row = await self.ensure_company(cik)
        row.filings_listed_at = checked_at

    # ── company_financials ───────────────────────────────────────────────────

    async def replace_rows(
        self,
        cik: int,
        rows: Sequence[dict],
        fetched_at: datetime,
        entity_name: Optional[str] = None,
        splits: Sequence[dict] = (),
    ) -> None:
        """
        Every figure of one company, replaced as one set with the splits its filings
        restated history for; clears its miss stamps.
        """
        company = await self.ensure_company(cik)
        if entity_name and not company.name:
            company.name = entity_name
        company.filed_splits = json.dumps(list(splits)) if splits else None
        await self._replace(company, rows, fetched_at)
        company.fetched_at = fetched_at
        company.facts_checked_at = None
        company.facts_miss_count = None

    async def replace_vendor_rows(
        self, cik: int, rows: Sequence[dict], fetched_at: datetime
    ) -> None:
        """This company's Alpha Vantage rows (`alpha_vantage:` sources), replaced as one
        set; SEC's rows are not touched."""
        await self.session.execute(
            delete(CompanyFinancial).where(
                CompanyFinancial.cik == cik,
                CompanyFinancial.source_concept.like("alpha_vantage:%"),
            )
        )
        self.session.add_all(
            CompanyFinancial(cik=cik, fetched_at=fetched_at, **row) for row in rows
        )

    # ── vendor_statements ────────────────────────────────────────────────────

    async def vendor_statements(self, cik: int) -> Dict[str, dict]:
        """function -> the stored statement (`company_vendor_fill.compact`)."""
        result = await self.session.execute(
            select(VendorStatement).where(VendorStatement.cik == cik)
        )
        return {row.function: row.body for row in result.scalars()}

    async def write_vendor_statement(
        self, cik: int, function: str, body: dict, fetched_at: datetime
    ) -> None:
        await self.ensure_company(cik)
        await self.session.merge(
            VendorStatement(cik=cik, function=function, body=body, fetched_at=fetched_at)
        )

    # ── sec_filing_reads ─────────────────────────────────────────────────────

    async def filing_reads(self, cik: int) -> Dict[str, SecFilingRead]:
        """Every filing of this company whose instance was read, by accession number."""
        result = await self.session.execute(
            select(SecFilingRead).where(SecFilingRead.cik == cik)
        )
        return {row.accn: row for row in result.scalars()}

    async def record_filing_read(
        self,
        cik: int,
        accn: str,
        form: str,
        filed: date,
        facts: Sequence[dict],
        read_at: datetime,
        reader: int,
    ) -> None:
        """What one filing held, nothing included, and the `reader` that read it: the
        filing is never asked again for what that reader looks for. A read by an older
        reader is replaced whole — the new one looked for everything the old one did."""
        await self.ensure_company(cik)
        await self.session.merge(
            SecFilingRead(
                cik=cik,
                accn=accn,
                form=form,
                filed=filed,
                facts=list(facts),
                read_at=read_at,
                reader=reader,
            )
        )

    async def finding_rows(self, cik: int) -> List[Dict]:
        """The columns the pass's checks read (`ttm_dividend_gap`, the newest period), as
        the normaliser's row dicts carry them — no unit, concept or filing date."""
        result = await self.session.execute(
            select(
                CompanyFinancial.period_end,
                CompanyFinancial.fiscal_period,
                CompanyFinancial.metric,
                CompanyFinancial.value,
            ).where(CompanyFinancial.cik == cik)
        )
        return [
            {"period_end": p, "fiscal_period": fp, "metric": m, "value": v}
            for p, fp, m, v in result.all()
        ]

    async def rows_for(self, cik: int) -> List[CompanyFinancial]:
        result = await self.session.execute(
            select(CompanyFinancial)
            .where(CompanyFinancial.cik == cik)
            .order_by(CompanyFinancial.period_end, CompanyFinancial.fiscal_period)
        )
        return list(result.scalars())

    async def rows_of(self, company) -> List[CompanyFinancial]:
        """A company's figures, an SEC filer's or another regulator's."""
        result = await self.session.execute(
            select(CompanyFinancial)
            .where(_owned_by(company))
            .order_by(CompanyFinancial.period_end, CompanyFinancial.fiscal_period)
        )
        return list(result.scalars())

    async def coverage(self, ciks: Iterable[int]) -> Dict[int, dict]:
        """Per SEC filer with rows: first and last period end, newest fetch, row count, and
        the most common monetary unit (its reporting currency)."""
        return await self._coverage(CompanyFinancial.cik, ciks)

    async def coverage_of(self, companies: Iterable) -> Dict[str, dict]:
        """`coverage` for company objects of any regulator, by `key`."""
        sec, other = {}, {}
        for company in companies:
            if company.cik is not None:
                sec[company.cik] = company.key
            else:
                other[company.company_id] = company.key
        out = {
            sec[cik]: entry
            for cik, entry in (await self._coverage(CompanyFinancial.cik, sec)).items()
        }
        found = await self._coverage(CompanyFinancial.company_id, other)
        out.update({other[cid]: entry for cid, entry in found.items()})
        return out

    async def _coverage(self, column, owners: Iterable[int]) -> Dict[int, dict]:
        wanted = list(owners)
        if not wanted:
            return {}
        result = await self.session.execute(
            select(
                column,
                func.min(CompanyFinancial.period_end),
                func.max(CompanyFinancial.period_end),
                func.max(CompanyFinancial.fetched_at),
                func.count(CompanyFinancial.id),
            )
            .where(column.in_(wanted))
            .group_by(column)
        )
        out = {
            owner: {"first": first, "last": last, "fetched_at": fetched, "rows": n}
            for owner, first, last, fetched, n in result.all()
        }
        # The currency of its revenue, or of its profit for a company that tags no revenue
        # line (a bank's ESEF report: HSBC, Santander; #85).
        units = await self.session.execute(
            select(column, CompanyFinancial.metric, CompanyFinancial.unit, func.count())
            .where(
                column.in_(list(out)),
                CompanyFinancial.metric.in_(CURRENCY_METRICS),
            )
            .group_by(column, CompanyFinancial.metric, CompanyFinancial.unit)
        )
        best: Dict[int, tuple] = {}
        for owner, metric, unit, n in units.all():
            rank = (-CURRENCY_METRICS.index(metric), n)
            if owner not in best or rank > best[owner][1]:
                best[owner] = (unit, rank)
        for owner, entry in out.items():
            entry["currency"] = best.get(owner, (None,))[0]
        return out

    # ── company_prices, company_splits ───────────────────────────────────────

    async def prices_for(self, cik: int) -> List[CompanyPrice]:
        result = await self.session.execute(
            select(CompanyPrice).where(CompanyPrice.cik == cik).order_by(CompanyPrice.date)
        )
        return list(result.scalars())

    async def splits_for(self, cik: int) -> List[CompanySplit]:
        result = await self.session.execute(
            select(CompanySplit).where(CompanySplit.cik == cik).order_by(CompanySplit.date)
        )
        return list(result.scalars())

    async def write_prices(
        self,
        cik: int,
        closes: Sequence[Tuple[date, float]],
        splits: Sequence[Tuple[date, float]],
        currency: str,
        fetched_at: datetime,
        since: Optional[date] = None,
    ) -> None:
        """
        One fetch's closes and splits as one set: everything from `since` on is replaced
        (the whole history when `since` is None), and the miss stamps are cleared.
        """
        company = await self.ensure_company(cik)
        for model in (CompanyPrice, CompanySplit):
            stale = delete(model).where(model.cik == cik)
            if since is not None:
                stale = stale.where(model.date >= since)
            await self.session.execute(stale)
        self.session.add_all(
            CompanyPrice(cik=cik, date=d, close=c, currency=currency) for d, c in closes
        )
        self.session.add_all(CompanySplit(cik=cik, date=d, ratio=r) for d, r in splits)
        company.prices_fetched_at = fetched_at
        company.prices_checked_at = None
        company.prices_miss_count = None

    # ── company_yahoo_profiles ────────────────────────────────────────────────

    async def yahoo_profiles(
        self, ciks: Iterable[int]
    ) -> Dict[int, CompanyYahooProfile]:
        wanted = list(ciks)
        if not wanted:
            return {}
        result = await self.session.execute(
            select(CompanyYahooProfile).where(CompanyYahooProfile.cik.in_(wanted))
        )
        return {row.cik: row for row in result.scalars()}

    async def write_yahoo_profile(
        self, cik: int, values: dict, fetched_at: datetime
    ) -> None:
        """One company's Yahoo figures, replaced as one set (a key absent is NULL)."""
        row = await self.session.get(CompanyYahooProfile, cik)
        if row is None:
            row = CompanyYahooProfile(cik=cik)
            self.session.add(row)
        for column in YAHOO_PROFILE_COLUMNS:
            setattr(row, column, values.get(column))
        row.fetched_at = fetched_at
        row.checked_at = None
        row.miss_count = None

    async def record_yahoo_miss(self, cik: int, checked_at: datetime) -> None:
        """Yahoo had nothing, or failed; the stored figures stay, the next ask waits."""
        row = await self.session.get(CompanyYahooProfile, cik)
        if row is None:
            row = CompanyYahooProfile(cik=cik)
            self.session.add(row)
        row.checked_at = checked_at
        row.miss_count = (row.miss_count or 0) + 1

    async def record_price_miss(self, cik: int, checked_at: datetime) -> None:
        """Yahoo had no closes for the company; `prices_backoff_until` spaces the next ask."""
        row = await self.ensure_company(cik)
        row.prices_checked_at = checked_at
        row.prices_miss_count = (row.prices_miss_count or 0) + 1
