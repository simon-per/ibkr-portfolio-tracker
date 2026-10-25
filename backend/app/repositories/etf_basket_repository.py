"""Repository for EtfBasket / EtfHolding — stored fund constituent baskets."""
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models.etf_basket import ConstituentIdentifierMiss, EtfBasket, EtfHolding

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
    # The parse matched the stored basket exactly, so no row was rewritten.
    unchanged: bool = False


def basket_fingerprint(parsed) -> str:
    """
    SHA-256 over everything a parse contributes to the stored rows, except its as-of.

    The as-of is left out on purpose: DWS and a clamped Defiance stand the fetch date in for
    one, so the same file fetched on two days must still read as the same file.
    """
    payload = [
        parsed.source, parsed.adapter, parsed.source_rows, parsed.skipped_rows,
        parsed.asset_class_available,
        [
            [r.line_no, r.isin, r.identifier, r.ticker, r.name, str(r.weight_pct),
             r.asset_class, r.sector, r.country]
            for r in parsed.rows
        ],
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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

    async def fund_isins(self, ref: str) -> Set[str]:
        """
        Where a fund's basket is stored, for a reference to the fund — its ISIN, or the
        symbol of a security that is one: those ISINs, and the basket each borrows
        (`FundSource.basket_proxy_isin`). The logo pass's `--basket` and the EDGAR pass's
        Nasdaq-100 read a fund through this one answer.
        """
        from app.etf_sources import FUND_SOURCES
        from app.models.security import Security

        ref = ref.strip().upper()
        isins = {ref} | set((await self.session.execute(
            select(Security.isin).where(Security.symbol == ref, Security.isin.is_not(None))
        )).scalars())
        return isins | {
            FUND_SOURCES[f].basket_proxy_isin for f in list(isins)
            if f in FUND_SOURCES and FUND_SOURCES[f].basket_proxy_isin
        }

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

    async def resolved_figis(self, identifiers: Iterable[Optional[str]]) -> Dict[str, str]:
        """
        The shareClassFIGI already resolved for each identifier, from any fund's holdings.

        One identifier names one security whichever fund lists it, so an answer stamped on a
        GRID row serves the same CINS in SOXQ. Ties break on the lowest FIGI so the result
        does not depend on row order.
        """
        wanted = {i.strip().upper() for i in identifiers if i and i.strip()}
        if not wanted:
            return {}
        rows = (
            await self.session.execute(
                select(EtfHolding.constituent_identifier, EtfHolding.constituent_share_class_figi)
                .where(
                    EtfHolding.constituent_identifier.in_(wanted),
                    EtfHolding.constituent_share_class_figi.isnot(None),
                )
                .distinct()
            )
        ).all()
        found: Dict[str, str] = {}
        for identifier, figi in rows:
            key = identifier.strip().upper()
            if key not in found or figi < found[key]:
                found[key] = figi
        return found

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

    async def identifier_misses_since(self, since: datetime) -> Set[str]:
        """Identifiers OpenFIGI answered with nothing at or after `since`."""
        rows = (
            await self.session.execute(
                select(ConstituentIdentifierMiss.identifier)
                .where(ConstituentIdentifierMiss.checked_at >= since)
            )
        ).scalars().all()
        return set(rows)

    async def record_identifier_misses(self, identifiers: Iterable[str], at: datetime) -> None:
        """Remember a definitive "no match", restamping one already known. Flushed only."""
        rows = [{"identifier": i, "checked_at": at} for i in sorted(set(identifiers))]
        for start in range(0, len(rows), self.INSERT_CHUNK):
            stmt = sqlite_insert(ConstituentIdentifierMiss).values(
                rows[start:start + self.INSERT_CHUNK]
            )
            await self.session.execute(stmt.on_conflict_do_update(
                index_elements=["identifier"], set_={"checked_at": stmt.excluded.checked_at},
            ))
        await self.session.flush()

    async def forget_identifier_misses(self, identifiers: Iterable[str]) -> None:
        """Drop the miss for identifiers that have since resolved. Flushed only."""
        wanted = sorted(set(identifiers))
        for start in range(0, len(wanted), self.SELECT_CHUNK):
            chunk = wanted[start:start + self.SELECT_CHUNK]
            await self.session.execute(
                delete(ConstituentIdentifierMiss)
                .where(ConstituentIdentifierMiss.identifier.in_(chunk))
            )
        await self.session.flush()

    async def mark_checked(self, fund_isin: str) -> None:
        """
        Stamp an attempt that stored nothing — a fetch error, a parse failure, a refusal — so
        the fund waits out its fetch interval like any other. A fund with no basket row has
        nothing to stamp and is asked again next evening. Flushed only.
        """
        await self.session.execute(
            update(EtfBasket)
            .where(EtfBasket.fund_isin == fund_isin.strip().upper())
            .values(checked_at=utcnow())
        )
        await self.session.flush()

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

        **Identical content is not rewritten** (`basket_fingerprint` matches the stored one):
        only `checked_at` moves, and `unchanged` tells the caller there is nothing new for
        the CINS/SEDOL pass. The daily refresh meets this every weekend and holiday. The as-of
        moves forward only when the issuer states it; a stood-in date (DWS, a clamped
        Defiance) stays where it was, because erring older is the safe direction for a
        staleness alarm.
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

        fingerprint = basket_fingerprint(parsed)
        if previous is not None and previous.content_sha256 == fingerprint:
            previous.checked_at = utcnow()
            if parsed.as_of_is_issuer_stated and parsed.as_of_date > previous.as_of_date:
                previous.as_of_date = parsed.as_of_date
                previous.as_of_is_issuer_stated = True
            await self.session.flush()
            return ReplaceResult(
                fund_isin=fund_isin, stored_rows=previous_rows, previous_rows=previous_rows,
                previous_as_of=previous_as_of, unchanged=True,
            )

        # Read before the delete, which removes this fund's own earlier answers.
        known_figis = await self.resolved_figis(row.identifier for row in parsed.rows)

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
                # Never taken from the parse — it is OpenFIGI's answer — but OpenFIGI's
                # earlier answer for the same identifier still holds: a CINS or SEDOL names
                # one security for good. Clearing it on every replace undid the Invesco
                # funds' resolutions each evening, faster than the bounded pass restores them.
                "constituent_share_class_figi": (
                    known_figis.get(row.identifier.strip().upper()) if row.identifier else None
                ),
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
            "checked_at": utcnow(),
            "content_sha256": fingerprint,
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
