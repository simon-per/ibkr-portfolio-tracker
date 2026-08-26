from typing import List, Optional
from datetime import date, datetime
from sqlalchemy import select, and_, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dividend_payment import DividendPayment

YFINANCE_SOURCE = "yfinance_estimate"


class DividendRepository:
    """Repository for DividendPayment model operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_estimates_for_security(self, security_id: int) -> List[DividendPayment]:
        """The yfinance-derived rows only — the ones a ticker change invalidates."""
        result = await self.session.execute(
            select(DividendPayment)
            .where(
                and_(
                    DividendPayment.security_id == security_id,
                    DividendPayment.source == YFINANCE_SOURCE,
                )
            )
            .order_by(DividendPayment.ex_date.asc())
        )
        return list(result.scalars().all())

    async def delete_estimates_for_security(self, security_id: int) -> int:
        """
        Drop this security's yfinance-derived dividend rows. Returns the count.

        Deliberately **never** touches an ``ibkr`` row: those come from the Flex
        cash-transaction ledger, carry real withholding, and no ticker mapping can
        invalidate them. The estimates are keyed to whatever Yahoo ticker was
        resolved when they were written, so when that mapping turns out to have
        been wrong they are the poisoned half — see the SBI case, where two rows
        from a bare-ticker US listing survived the mapping fix and the price purge,
        and went on defining a gold miner's dividend schedule.
        """
        result = await self.session.execute(
            delete(DividendPayment).where(
                and_(
                    DividendPayment.security_id == security_id,
                    DividendPayment.source == YFINANCE_SOURCE,
                )
            )
        )
        return result.rowcount or 0

    async def get_by_security(self, security_id: int) -> List[DividendPayment]:
        result = await self.session.execute(
            select(DividendPayment)
            .where(DividendPayment.security_id == security_id)
            .order_by(DividendPayment.ex_date.desc())
        )
        return list(result.scalars().all())

    async def get_latest_ex_date(self, security_id: int) -> Optional[date]:
        result = await self.session.execute(
            select(func.max(DividendPayment.ex_date))
            .where(DividendPayment.security_id == security_id)
        )
        return result.scalar_one_or_none()

    async def upsert_payment(self, data: dict) -> DividendPayment:
        # Scoped by source, matching uix_dividend_security_source_exdate. Without
        # it a yfinance ex-date landing on an IBKR pay date resolved to the *other*
        # source's row and overwrote it — see the constraint's comment. Both
        # callers already pass a source; default defensively rather than matching
        # every source at once, which would reintroduce the collision.
        source = data.get('source') or YFINANCE_SOURCE
        result = await self.session.execute(
            select(DividendPayment).where(
                and_(
                    DividendPayment.security_id == data['security_id'],
                    DividendPayment.source == source,
                    DividendPayment.ex_date == data['ex_date'],
                )
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            for key, value in data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await self.session.flush()
            return existing
        else:
            payment = DividendPayment(**data)
            self.session.add(payment)
            await self.session.flush()
            return payment

    async def earliest_ibkr_payment_date(self) -> Optional[date]:
        """
        Date of the first authoritative IBKR payment — the era-splice boundary, taken
        from the WHOLE table. `None` when no IBKR rows exist yet.

        Exists because a caller that works in a window cannot derive this itself.
        `DividendService._splice_by_era` computes the boundary from the rows it is
        handed, which is right for the readers that splice the full history and wrong
        for the activity ledger, which windows first: fed a slice, it would treat the
        slice's earliest IBKR row as the start of the era and keep estimates that a
        real IBKR row already supersedes. Same shape and same reason as
        `CashFlowRepository.earliest_flow_date()`.

        Uses `coalesce(pay_date, ex_date)` exactly as `has_ibkr_dividends` does — the
        two must never disagree about which date a payment belongs to, since yfinance
        stores an ex-date and IBKR a pay date weeks apart.
        """
        on_date = func.coalesce(DividendPayment.pay_date, DividendPayment.ex_date)
        result = await self.session.execute(
            select(func.min(on_date)).where(DividendPayment.source == "ibkr")
        )
        return result.scalar()

    async def get_ibkr_payments(self) -> List[DividendPayment]:
        """
        Every authoritative IBKR payment, whole table, ordered by pay date.

        Deliberately narrower than `get_between`/`get_computed_dividends`: those serve
        the readers that must splice the two sources, and this one serves the cash
        balance, where a `yfinance_estimate` represents money paid into a *different*
        broker and must never be added. `DividendService.ibkr_cash_receipts` is the
        only caller and carries the full reasoning.
        """
        on_date = func.coalesce(DividendPayment.pay_date, DividendPayment.ex_date)
        result = await self.session.execute(
            select(DividendPayment)
            .where(DividendPayment.source == "ibkr")
            .order_by(on_date.asc())
        )
        return list(result.scalars().all())

    async def get_computed_dividends(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        source: Optional[str] = None,
    ) -> List[DividendPayment]:
        stmt = select(DividendPayment).where(
            DividendPayment.gross_amount_eur.isnot(None)
        )
        if source is not None:
            stmt = stmt.where(DividendPayment.source == source)
        if start_date:
            stmt = stmt.where(DividendPayment.ex_date >= start_date)
        if end_date:
            stmt = stmt.where(DividendPayment.ex_date <= end_date)
        stmt = stmt.order_by(DividendPayment.ex_date.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_between(self, start: date, end: date) -> List[DividendPayment]:
        """
        Payments landing in [start, end], windowed on **pay_date falling back to
        ex_date** — the same `coalesce` `has_ibkr_dividends` uses.

        That coalesce is the point. yfinance rows are stored under the ex-date and IBKR
        rows under the pay date (the divergence that forced the source-aware key on this
        table), and a payment with Mastercard's 29-day lag would fall in a different
        window depending on which column was asked. A ledger is about when the cash
        moved, so pay_date wins wherever it exists.
        """
        on_date = func.coalesce(DividendPayment.pay_date, DividendPayment.ex_date)
        result = await self.session.execute(
            select(DividendPayment)
            .where(and_(on_date >= start, on_date <= end))
            .order_by(on_date.asc())
        )
        return list(result.scalars().all())

    async def has_ibkr_dividends(
        self, start: Optional[date] = None, end: Optional[date] = None
    ) -> bool:
        """
        True if authoritative IBKR-sourced dividend rows exist, optionally only within
        [start, end].

        Callers that report per year MUST pass the window. A Flex Query only returns cash
        transactions inside its period, so a year-to-date sync leaves earlier years with
        estimates only — asking globally would make a prior-year report filter to `ibkr`,
        find nothing, and present 0.00 as authoritative.

        The window uses `pay_date` falling back to `ex_date`, matching how TaxService
        buckets a payment into a year, so the flag can never disagree with the rows shown.
        """
        stmt = select(func.count(DividendPayment.id)).where(DividendPayment.source == "ibkr")
        on_date = func.coalesce(DividendPayment.pay_date, DividendPayment.ex_date)
        if start is not None:
            stmt = stmt.where(on_date >= start)
        if end is not None:
            stmt = stmt.where(on_date <= end)
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0) > 0

    async def get_uncomputed(self) -> List[DividendPayment]:
        result = await self.session.execute(
            select(DividendPayment).where(
                DividendPayment.shares_held.is_(None)
            ).order_by(DividendPayment.ex_date.asc())
        )
        return list(result.scalars().all())

    async def get_last_fetch_time(self, security_id: int) -> Optional[datetime]:
        result = await self.session.execute(
            select(func.max(DividendPayment.created_at))
            .where(DividendPayment.security_id == security_id)
        )
        return result.scalar_one_or_none()
