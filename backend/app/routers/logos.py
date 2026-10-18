"""
Holding logos, served from our own origin (docs/logos.md).

Reads only: the write paths are the logo pass (`logo_service`) and the two CLIs — there is
no upload endpoint. The manifest tells the browser which securities have a logo, so it never
asks for one that does not exist; the image URL carries the version from the manifest, so
it can be cached forever and only a first visit spends requests against the rate limit.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.security_logo import SecurityLogo
from app.routers._params import DbId
from app.repositories.security_logo_repository import VERSION_CHARS, SecurityLogoRepository

router = APIRouter()

IMMUTABLE = "public, max-age=31536000, immutable"


@router.get("")
async def logo_manifest(db: AsyncSession = Depends(get_db)):
    """
    `{"logos": {"<security_id>": "<version>"}, "isins": {"<isin>": "<version>"}}`: every
    security with a logo, and every stored image by ISIN — what a look-through constituent
    or a watchlist row, which carry an ISIN but no security, is drawn with.
    """
    repo = SecurityLogoRepository(db)
    manifest = await repo.manifest()
    return {
        "logos": {str(sid): version for sid, version in sorted(manifest.items())},
        "isins": dict(sorted((await repo.isin_manifest()).items())),
    }


@router.get("/isin/{isin}")
async def logo_image_by_isin(
    isin: str, request: Request, v: Optional[str] = None, db: AsyncSession = Depends(get_db),
):
    """The stored bytes by ISIN, with the same caching as by security."""
    return _image(await SecurityLogoRepository(db).for_isin(isin), request, v)


@router.get("/{security_id}")
async def logo_image(
    security_id: DbId, request: Request, v: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """The stored bytes of a security's logo."""
    return _image(await SecurityLogoRepository(db).for_security(security_id), request, v)


def _image(row: Optional[SecurityLogo], request: Request, v: Optional[str]) -> Response:
    """
    Cached for a year only when `v` names the current version — a stale `v` (the logo was
    replaced since the manifest was read) gets the new bytes uncached, so no browser pins an
    old image under a URL that now means a new one.
    """
    if row is None:
        return Response(status_code=404)
    etag = f'"{row.sha256}"'
    headers = {
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": IMMUTABLE if v == row.sha256[:VERSION_CHARS] else "no-cache",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=row.image, media_type=row.content_type, headers=headers)
