"""
Cached company identity for one ISIN.

Two keyless providers answer this, and the measurements against this account's own ISINs
(2026-08-14) show they are complementary rather than redundant, which is why both are
stored side by side:

- **GLEIF** (`api.gleif.org`, ISIN -> the issuing entity's LEI) folds share classes and
  multi-ISIN companies. Both Alphabet ISINs return 5493006MHB84DD0ZWV18; both ASML ISINs
  return 724500Y6DUVHQD6OXN27.
- **OpenFIGI** (`shareClassFIGI`) folds one share class across venues, and is the only
  one that answers at all for the Taiwanese and Korean ordinaries held here — GLEIF has
  no ISIN record for TSMC, Samsung, SK Hynix or Credo.

LEIs and share-class FIGIs do not change, so a positive answer is cached indefinitely.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IsinIdentity(Base):
    """
    One row per ISIN. Absence of a row means *never asked*; a NULL identifier beside a
    non-NULL `*_checked_at` means *asked, and that provider has nothing*.

    **That distinction is the whole reason the two providers get separate status
    columns.** Without it every run re-asks the same unresolvable ISINs forever — four
    of this account's ~20 direct ISINs are permanently absent from GLEIF — which is the
    shape `get_missing_dates()`'s holiday rule and `UPSTREAM_RETRY_COOLDOWN_SECONDS`
    both exist to stop. And because they fail independently in *both* directions (GLEIF
    resolves ETF ISINs that are not equities; OpenFIGI resolves the ordinaries GLEIF
    misses), one combined status could not tell "ask GLEIF again" from "ask OpenFIGI
    again".
    """
    __tablename__ = "isin_identities"

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)

    # --- GLEIF ---
    lei: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    issuer_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # A bulk-file LEI is not an API LEI, and the provenance column is the one every
    # diagnosis reads first — market_prices carried 'yahoo_finance' on Alpha Vantage
    # rows for exactly this reason. 'gleif_api' | 'gleif_bulk'.
    lei_source: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    lei_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # --- OpenFIGI ---
    share_class_figi: Mapped[Optional[str]] = mapped_column(
        String(12), nullable=True, index=True
    )
    # Stored for diagnosis and deliberately NOT an input to grouping: compositeFIGI is
    # per-exchange-composite, so it folds nothing the ISIN does not already fold, and
    # unioning on it would silently merge unrelated lines. `company_identity` names it
    # as excluded so a reader who finds it here does not "complete" the ladder.
    composite_figi: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    figi_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    figi_source: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    figi_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<IsinIdentity {self.isin} lei={self.lei} "
            f"scfigi={self.share_class_figi}>"
        )
