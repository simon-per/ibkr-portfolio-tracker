"""
Which companies beyond the SEC the Company tab carries (docs/company-financials.md, *Markets
beyond the SEC*): the companies the app names — the look-through's heaviest
`company_financials_sync.NAMED_LOOKTHROUGH` and every held listing — with their ISINs and LEI.
Each market's pass takes the ones its regulator holds (Korean ISINs for OpenDART, an LEI for
ESEF) and skips any company that already has an SEC page. Pure DB.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.etf_mappings import is_known_etf_isin
from app.models.company_financials import SecCompany, SecurityCik
from app.models.isin_identity import IsinIdentity
from app.models.security import Security
from app.repositories.security_repository import held


@dataclass
class NamedCompany:
    name: str
    isins: List[str] = field(default_factory=list)
    lei: Optional[str] = None
    held: bool = False


async def named_companies(db: AsyncSession) -> List[NamedCompany]:
    """
    The look-through's heaviest companies, heaviest first, then any held company not among
    them; each with every ISIN the app knows it by and its LEI (the identity store's). One
    company once: a held listing whose ISIN a row already names joins that row.
    """
    from app.services.company_financials_sync import NAMED_LOOKTHROUGH
    from app.services.lookthrough_service import LookthroughService

    view = await LookthroughService(db).get_lookthrough(limit=NAMED_LOOKTHROUGH)
    out: List[NamedCompany] = []
    by_isin: Dict[str, NamedCompany] = {}
    for row in view.get("companies") or []:
        isins = sorted({i.strip().upper() for i in row.get("isins") or [] if i})
        if not isins:
            continue
        lei = row.get("company_key") if row.get("key_type") == "lei" else None
        company = NamedCompany(row.get("name") or isins[0], isins, lei)
        out.append(company)
        for isin in isins:
            by_isin.setdefault(isin, company)
    held_rows = (
        (
            await db.execute(
                select(Security).where(held()).order_by(Security.symbol, Security.id)
            )
        )
        .scalars()
        .all()
    )
    for security in held_rows:
        isin = (security.isin or "").strip().upper()
        if not isin or is_known_etf_isin(isin):
            continue
        company = by_isin.get(isin)
        if company is None:
            company = NamedCompany(security.description or security.symbol, [isin])
            out.append(company)
            by_isin[isin] = company
        company.held = True
    missing = [c for c in out if c.lei is None]
    if missing:
        leis = dict(
            (
                await db.execute(
                    select(IsinIdentity.isin, IsinIdentity.lei).where(
                        IsinIdentity.isin.in_([i for c in missing for i in c.isins]),
                        IsinIdentity.lei.is_not(None),
                    )
                )
            ).all()
        )
        for company in missing:
            company.lei = next((leis[i] for i in company.isins if i in leis), None)
    return out


async def sec_isins(db: AsyncSession) -> set:
    """Every ISIN an SEC page already answers for: a company's own, and its CIK-mapped held
    listings'. A company with an SEC page keeps it (a 20-F is its own annual report)."""
    found = set(
        i.strip().upper()
        for i in (
            await db.execute(
                select(SecCompany.isin).where(SecCompany.isin.is_not(None))
            )
        ).scalars()
    )
    found |= set(
        (i or "").strip().upper()
        for i in (
            await db.execute(
                select(Security.isin)
                .join(SecurityCik, SecurityCik.security_id == Security.id)
                .where(SecurityCik.cik.is_not(None))
            )
        ).scalars()
    )
    return found - {""}
