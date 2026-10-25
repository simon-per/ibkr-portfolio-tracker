"""
Reads and writes of `company_financials`, `sec_companies`, `security_ciks` and the
company prices (docs/company-financials.md).

`replace_rows` is the only write of a company's figures and it **refuses whole**: the
company's rows are deleted and the new set inserted in one transaction, so a reader never
sees half of one fetch beside half of the previous one. `write_prices` does the same for
the stretch of closes one fetch covers.
"""

from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_financials import (
    CompanyFinancial,
    CompanyPrice,
    CompanySplit,
    SecCompany,
    SecTicker,
    SecurityCik,
)
from app.services.miss_backoff import miss_backoff_until


def facts_backoff_until(row: Optional[SecCompany]) -> Optional[datetime]:
    """When companyfacts may next be asked for this company, or None if it may now."""
    if row is None:
        return None
    return miss_backoff_until(row.facts_checked_at, row.facts_miss_count)


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
        self, ticker: str, cik: Optional[int], checked_at: datetime
    ) -> None:
        row = await self.session.get(SecTicker, ticker)
        if row is None:
            row = SecTicker(ticker=ticker, checked_at=checked_at)
            self.session.add(row)
        row.cik, row.checked_at = cik, checked_at

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

    async def record_miss(self, cik: int, checked_at: datetime) -> None:
        """companyfacts had nothing this app reads; `facts_backoff_until` spaces the next ask."""
        row = await self.ensure_company(cik)
        row.facts_checked_at = checked_at
        row.facts_miss_count = (row.facts_miss_count or 0) + 1

    # ── company_financials ───────────────────────────────────────────────────

    async def replace_rows(
        self,
        cik: int,
        rows: Sequence[dict],
        fetched_at: datetime,
        entity_name: Optional[str] = None,
    ) -> None:
        """Every figure of one company, replaced as one set; clears its miss stamps."""
        company = await self.ensure_company(cik)
        if entity_name and not company.name:
            company.name = entity_name
        await self.session.execute(
            delete(CompanyFinancial).where(CompanyFinancial.cik == cik)
        )
        self.session.add_all(
            CompanyFinancial(cik=cik, fetched_at=fetched_at, **row) for row in rows
        )
        company.fetched_at = fetched_at
        company.facts_checked_at = None
        company.facts_miss_count = None

    async def rows_for(self, cik: int) -> List[CompanyFinancial]:
        result = await self.session.execute(
            select(CompanyFinancial)
            .where(CompanyFinancial.cik == cik)
            .order_by(CompanyFinancial.period_end, CompanyFinancial.fiscal_period)
        )
        return list(result.scalars())

    async def coverage(self, ciks: Iterable[int]) -> Dict[int, dict]:
        """Per company with rows: first and last period end, newest fetch, row count, and
        the most common monetary unit (its reporting currency)."""
        wanted = list(ciks)
        if not wanted:
            return {}
        result = await self.session.execute(
            select(
                CompanyFinancial.cik,
                func.min(CompanyFinancial.period_end),
                func.max(CompanyFinancial.period_end),
                func.max(CompanyFinancial.fetched_at),
                func.count(CompanyFinancial.id),
            )
            .where(CompanyFinancial.cik.in_(wanted))
            .group_by(CompanyFinancial.cik)
        )
        out = {
            cik: {"first": first, "last": last, "fetched_at": fetched, "rows": n}
            for cik, first, last, fetched, n in result.all()
        }
        units = await self.session.execute(
            select(CompanyFinancial.cik, CompanyFinancial.unit, func.count())
            .where(
                CompanyFinancial.cik.in_(list(out)),
                CompanyFinancial.metric == "revenue",
            )
            .group_by(CompanyFinancial.cik, CompanyFinancial.unit)
        )
        best: Dict[int, tuple] = {}
        for cik, unit, n in units.all():
            if cik not in best or n > best[cik][1]:
                best[cik] = (unit, n)
        for cik, entry in out.items():
            entry["currency"] = best.get(cik, (None,))[0]
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

    async def record_price_miss(self, cik: int, checked_at: datetime) -> None:
        """Yahoo had no closes for the company; `prices_backoff_until` spaces the next ask."""
        row = await self.ensure_company(cik)
        row.prices_checked_at = checked_at
        row.prices_miss_count = (row.prices_miss_count or 0) + 1
