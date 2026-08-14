"""Repository for EtfBasket / EtfHolding — stored fund constituent baskets."""
import logging
from typing import Dict, Iterable, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.etf_basket import EtfBasket, EtfHolding

logger = logging.getLogger(__name__)


class EtfBasketRepository:
    """Repository for EtfBasket / EtfHolding model operations"""

    SELECT_CHUNK = 400

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _norm(isins: Iterable[str]) -> List[str]:
        return sorted({i.strip().upper() for i in isins if i and i.strip()})

    async def get_baskets(self, fund_isins: Iterable[str]) -> Dict[str, EtfBasket]:
        """Fund ISIN -> basket metadata, for the funds that have a stored basket."""
        wanted = self._norm(fund_isins)
        out: Dict[str, EtfBasket] = {}
        for start in range(0, len(wanted), self.SELECT_CHUNK):
            chunk = wanted[start:start + self.SELECT_CHUNK]
            rows = (
                await self.session.execute(
                    select(EtfBasket).where(EtfBasket.fund_isin.in_(chunk))
                )
            ).scalars().all()
            for row in rows:
                out[row.fund_isin] = row
        return out

    async def get_holdings(
        self, fund_isins: Iterable[str]
    ) -> Dict[str, List[EtfHolding]]:
        """
        Fund ISIN -> its constituent rows, ordered by the source file's own row index.

        Ordered by `line_no` rather than by weight so the aggregation is reproducible and
        a diff against the issuer file reads in the same order the file did.
        """
        wanted = self._norm(fund_isins)
        out: Dict[str, List[EtfHolding]] = {isin: [] for isin in wanted}
        for start in range(0, len(wanted), self.SELECT_CHUNK):
            chunk = wanted[start:start + self.SELECT_CHUNK]
            rows = (
                await self.session.execute(
                    select(EtfHolding)
                    .where(EtfHolding.fund_isin.in_(chunk))
                    .order_by(EtfHolding.fund_isin.asc(), EtfHolding.line_no.asc())
                )
            ).scalars().all()
            for row in rows:
                out.setdefault(row.fund_isin, []).append(row)
        return out
