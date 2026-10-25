"""
Reads and writes of `company_financials` and `security_ciks` (docs/company-financials.md).

`replace_rows` is the only write of a security's figures and it **refuses whole**: the
security's rows are deleted and the new set inserted in one transaction, so a reader never
sees half of one fetch beside half of the previous one.
"""

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_financials import CompanyFinancial, SecurityCik
from app.services.miss_backoff import miss_backoff_until


def facts_backoff_until(row: Optional[SecurityCik]) -> Optional[datetime]:
    """When companyfacts may next be asked for this security, or None if it may now."""
    if row is None:
        return None
    return miss_backoff_until(row.facts_checked_at, row.facts_miss_count)


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
        if row.cik != cik:
            # A different company now: its stamps and figures are not this one's.
            row.facts_checked_at = None
            row.facts_miss_count = None
        row.cik, row.sec_ticker, row.cik_checked_at = cik, ticker, checked_at
        return row

    async def record_miss(
        self, security_ids: Sequence[int], checked_at: datetime
    ) -> None:
        """companyfacts had nothing this app reads; `facts_backoff_until` spaces the next ask."""
        for row in (await self.cik_rows(security_ids)).values():
            row.facts_checked_at = checked_at
            row.facts_miss_count = (row.facts_miss_count or 0) + 1

    # ── company_financials ───────────────────────────────────────────────────

    async def replace_rows(
        self, security_id: int, rows: Sequence[dict], fetched_at: datetime
    ) -> None:
        """Every figure of one security, replaced as one set; clears its miss stamps."""
        await self.session.execute(
            delete(CompanyFinancial).where(CompanyFinancial.security_id == security_id)
        )
        self.session.add_all(
            CompanyFinancial(security_id=security_id, fetched_at=fetched_at, **row)
            for row in rows
        )
        stamps = await self.session.get(SecurityCik, security_id)
        if stamps is not None:
            stamps.fetched_at = fetched_at
            stamps.facts_checked_at = None
            stamps.facts_miss_count = None

    async def rows_for(self, security_id: int) -> List[CompanyFinancial]:
        result = await self.session.execute(
            select(CompanyFinancial)
            .where(CompanyFinancial.security_id == security_id)
            .order_by(CompanyFinancial.period_end, CompanyFinancial.fiscal_period)
        )
        return list(result.scalars())

    async def coverage(self, security_ids: Iterable[int]) -> Dict[int, dict]:
        """Per security with rows: first and last period end, newest fetch, row count, and
        the most common monetary unit (its reporting currency)."""
        ids = list(security_ids)
        if not ids:
            return {}
        result = await self.session.execute(
            select(
                CompanyFinancial.security_id,
                func.min(CompanyFinancial.period_end),
                func.max(CompanyFinancial.period_end),
                func.max(CompanyFinancial.fetched_at),
                func.count(CompanyFinancial.id),
            )
            .where(CompanyFinancial.security_id.in_(ids))
            .group_by(CompanyFinancial.security_id)
        )
        out = {
            sid: {"first": first, "last": last, "fetched_at": fetched, "rows": n}
            for sid, first, last, fetched, n in result.all()
        }
        units = await self.session.execute(
            select(CompanyFinancial.security_id, CompanyFinancial.unit, func.count())
            .where(
                CompanyFinancial.security_id.in_(list(out)),
                CompanyFinancial.metric == "revenue",
            )
            .group_by(CompanyFinancial.security_id, CompanyFinancial.unit)
        )
        best: Dict[int, tuple] = {}
        for sid, unit, n in units.all():
            if sid not in best or n > best[sid][1]:
                best[sid] = (unit, n)
        for sid, entry in out.items():
            entry["currency"] = best.get(sid, (None,))[0]
        return out
