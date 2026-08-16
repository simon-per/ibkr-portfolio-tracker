"""Repository for EtfBasket / EtfHolding — stored fund constituent baskets."""
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models.etf_basket import EtfBasket, EtfHolding

logger = logging.getLogger(__name__)

# A replacement basket may not fall below this share of the rows already stored. The guard
# exists because the failure it prevents is the *quiet* one: `validate()` in the parsers
# catches an HTML body or a one-row file, but a genuinely partial parse of a 1,338-row fund
# yields hundreds of plausible rows, replaces the real basket atomically, and every
# look-through figure shrinks with nothing reporting it. Same shape as `reconcile_taxlots`'
# empty-statement wipe guard, and the same response: refuse and keep what we have.
MIN_ROW_RETENTION = Decimal("0.5")


class BasketReplaceRefused(Exception):
    """The incoming basket is implausible against the one already stored — nothing written."""


@dataclass
class ReplaceResult:
    fund_isin: str
    stored_rows: int
    previous_rows: int
    previous_as_of: Optional[date]


class EtfBasketRepository:
    """Repository for EtfBasket / EtfHolding model operations"""

    SELECT_CHUNK = 400
    INSERT_CHUNK = 100

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

    async def unresolved_identifiers(self) -> List[str]:
        """
        Distinct identifiers on holdings that have neither an ISIN nor a resolved FIGI.

        These are the CINS and SEDOL lines three of the US issuers publish instead of ISINs.
        Distinct rather than per-row because one identifier can appear in several funds — the
        same company inside GRID and QTUM is one question, not two.
        """
        rows = (
            await self.session.execute(
                select(EtfHolding.constituent_identifier)
                .where(
                    EtfHolding.constituent_identifier.isnot(None),
                    EtfHolding.constituent_isin.is_(None),
                    EtfHolding.constituent_share_class_figi.is_(None),
                )
                .distinct()
            )
        ).scalars().all()
        return sorted({r.strip().upper() for r in rows if r and r.strip()})

    async def apply_resolved_identifiers(self, figi_by_identifier: Dict[str, str]) -> int:
        """
        Stamp resolved shareClassFIGIs onto every holding carrying those identifiers.

        Writes the FIGI only — `constituent_isin` stays NULL and `constituent_identifier` stays
        as the issuer published it, so the row still says where each part of its identity came
        from. Flushed, not committed: the caller owns the transaction, as everywhere else here.
        """
        updated = 0
        for identifier, figi in figi_by_identifier.items():
            result = await self.session.execute(
                update(EtfHolding)
                .where(
                    EtfHolding.constituent_identifier == identifier,
                    EtfHolding.constituent_share_class_figi.is_(None),
                )
                .values(constituent_share_class_figi=figi)
            )
            updated += result.rowcount or 0
        await self.session.flush()
        return updated

    async def replace_basket(self, parsed, force: bool = False) -> ReplaceResult:
        """
        Replace one fund's basket wholesale, or refuse and change nothing.

        Wholesale rather than row-by-row because a basket is a snapshot: there is no sensible
        merge of two vintages, and `UNIQUE(fund_isin, line_no)` keys on the source file's own
        row order rather than on an identifier, so an upsert would silently mix them.

        Two refusals, both overridable with `force` for the case where the fund genuinely
        changed (an index reconstitution can halve a niche fund's row count):

        - **A collapse in row count** — see `MIN_ROW_RETENTION`.
        - **An as-of that moves backwards.** Re-importing an older saved file would regress
          the basket while looking like a successful refresh. Equal as-of is allowed, so
          re-running an import is idempotent.
        """
        fund_isin = parsed.fund_isin.strip().upper()
        previous = (await self.get_baskets([fund_isin])).get(fund_isin)
        previous_rows = 0
        previous_as_of = None

        if previous is not None:
            previous_as_of = previous.as_of_date
            previous_rows = int(previous.stored_rows or 0)
        if previous is not None and not force:
            if parsed.as_of_date < previous.as_of_date:
                raise BasketReplaceRefused(
                    f"{fund_isin}: the incoming basket is dated {parsed.as_of_date} but the "
                    f"stored one is {previous.as_of_date}. Refusing to move a basket "
                    f"backwards — pass --force if this is deliberate."
                )
            floor = Decimal(previous_rows) * MIN_ROW_RETENTION
            if previous_rows and Decimal(len(parsed.rows)) < floor:
                raise BasketReplaceRefused(
                    f"{fund_isin}: the incoming basket has {len(parsed.rows)} rows against "
                    f"{previous_rows} stored, below the {MIN_ROW_RETENTION:.0%} floor. A "
                    f"partial parse replaces a real basket with a plausible one and every "
                    f"figure silently shrinks, so nothing was written — pass --force if the "
                    f"fund genuinely shrank."
                )

        # Delete-then-insert inside the caller's transaction, so a failure part-way leaves the
        # previous basket intact rather than a half-replaced one.
        await self.session.execute(
            delete(EtfHolding).where(EtfHolding.fund_isin == fund_isin)
        )
        rows = [
            {
                "fund_isin": fund_isin,
                "line_no": row.line_no,
                "constituent_isin": row.isin,
                "constituent_identifier": row.identifier,
                # Never carried in from a parse: it is OpenFIGI's answer, and a
                # replacement basket has not been asked about yet.
                "constituent_share_class_figi": None,
                "constituent_ticker": row.ticker,
                "constituent_name": row.name,
                "weight_pct": row.weight_pct,
                "asset_class": row.asset_class,
                "sector": row.sector,
                "country": row.country,
            }
            for row in parsed.rows
        ]
        for start in range(0, len(rows), self.INSERT_CHUNK):
            self.session.add_all(
                EtfHolding(**values) for values in rows[start:start + self.INSERT_CHUNK]
            )
            await self.session.flush()

        fields = {
            "as_of_date": parsed.as_of_date,
            "as_of_is_issuer_stated": parsed.as_of_is_issuer_stated,
            "source": parsed.source,
            "adapter": parsed.adapter,
            "source_rows": parsed.source_rows,
            "stored_rows": len(parsed.rows),
            "skipped_rows": parsed.skipped_rows,
            "total_weight_pct": parsed.total_weight_pct,
            "equity_weight_pct": parsed.equity_weight_pct,
            "identifier_coverage_pct": parsed.identifier_coverage_pct,
            "asset_class_available": parsed.asset_class_available,
            "fetched_at": utcnow(),
        }
        if previous is None:
            self.session.add(EtfBasket(fund_isin=fund_isin, **fields))
        else:
            for key, value in fields.items():
                setattr(previous, key, value)
        await self.session.flush()

        return ReplaceResult(
            fund_isin=fund_isin,
            stored_rows=len(parsed.rows),
            previous_rows=previous_rows,
            previous_as_of=previous_as_of,
        )
