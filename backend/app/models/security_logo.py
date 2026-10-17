"""
A holding's logo mark, stored as bytes and served from our own origin (docs/logos.md).

Keyed by **ISIN**, so two listings of one company (ASML on NASDAQ and on Euronext) share
one image. The first `LargeBinary` in the schema: a 128 px PNG is a few kilobytes, and the
alternative — a file beside the database — would be a second thing to back up and restore.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

#: Fetched by `logo_service` from logo.dev's API.
LOGO_SOURCE_LOGO_DEV = "logo.dev"
#: Stored by hand with `app/cli/import_logo.py`. **The automatic pass never overwrites nor
#: re-asks about a manual row**: the owner put it there because logo.dev had nothing, or
#: had the wrong mark.
LOGO_SOURCE_MANUAL = "manual"


class SecurityLogo(Base):
    """
    One row per ISIN. Absence of a row means *never asked*; a NULL `image` beside a
    `checked_at` means *asked, and logo.dev had nothing usable* — the `IsinIdentity`
    pattern, so an ISIN logo.dev cannot serve is not re-asked (and re-billed) every pass.
    """
    __tablename__ = "security_logos"

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    image: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    # Only ever image/png, image/jpeg or image/webp — `logo_service.validate_logo`
    # refuses everything else, SVG above all (a same-origin SVG can carry script).
    content_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Hex digest of `image`: the ETag, and the first 12 characters version the URL.
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # When the current image was stored. NULL while there has never been one.
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # When logo.dev last answered about this ISIN (or the manual import ran).
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def __repr__(self) -> str:
        size = len(self.image) if self.image else 0
        return f"<SecurityLogo {self.isin} {self.source} {size}B>"
