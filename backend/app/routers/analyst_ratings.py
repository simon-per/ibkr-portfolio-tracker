"""
Analyst Ratings Router
API endpoints for syncing and retrieving analyst ratings.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.repositories.analyst_rating_repository import AnalystRatingRepository
from app.routers._params import DbId
from app.repositories.sync_run_repository import utc_iso
from app.models.analyst_rating import rating_of
from app.services.analyst_rating_service import AnalystRatingService
from app.services.company_yahoo import canonical_listings, counts_of
from app.single_flight import PUBLIC_SYNC_COOLDOWN_SECONDS, SYNC_PIPELINE, SyncBusy, single_flight


router = APIRouter()


@router.post("/sync")
async def sync_analyst_ratings(db: AsyncSession = Depends(get_db)):
    """
    Sync analyst ratings for all securities.

    This endpoint:
    1. Fetches all securities from the database
    2. For each security, fetches current analyst recommendations from Yahoo Finance
    3. Caches ratings in the database

    Uses yfinance library with rate limiting (1-3s per request, 2-4s between securities)

    Returns:
        Summary of synced data including counts and any errors
    """
    try:
        # Public route, one Yahoo request per security: one at a time, cooled down.
        with single_flight(SYNC_PIPELINE, cooldown_seconds=PUBLIC_SYNC_COOLDOWN_SECONDS):
            rating_service = AnalystRatingService(db)

            # Sync all securities
            result = await rating_service.sync_ratings_for_securities()

            if result['errors'] > 0:
                return {
                    "status": "partial_success",
                    **result
                }

            return {
                "status": "success",
                **result
            }

    except SyncBusy as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after_seconds)})
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync analyst ratings: {str(e)}"
        )


@router.post("/sync-stale")
async def sync_stale_analyst_ratings(db: AsyncSession = Depends(get_db)):
    """
    Sync only analyst ratings that are stale (`analyst_rating_repository.STALE_AFTER`)
    or missing, for held, Yahoo-eligible securities.

    The scheduler runs the same pass daily (`fundamentals_refresh`). It only fetches
    ratings that haven't been updated recently, reducing API calls and respecting rate
    limits.

    Returns:
        Summary of synced data
    """
    try:
        with single_flight(SYNC_PIPELINE, cooldown_seconds=PUBLIC_SYNC_COOLDOWN_SECONDS):
            rating_service = AnalystRatingService(db)
            result = await rating_service.sync_stale_ratings()

            return {
                "status": "success",
                **result
            }

    except SyncBusy as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after_seconds)})
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync stale analyst ratings: {str(e)}"
        )


@router.get("/status")
async def get_analyst_ratings_status(db: AsyncSession = Depends(get_db)):
    """
    Get status of analyst ratings cache.

    Returns:
        Statistics about cached ratings including total count and freshness
    """
    from sqlalchemy import select, func
    from app.models.analyst_rating import AnalystRating

    # Every figure here covers the rows of securities the sync walks
    # (`AnalystRatingRepository.in_scope`), so `fresh = total - stale` stays coherent.
    # The stale count goes through the repository: this route ran its own 3-day query
    # over every row, so a sold position's rating — never refreshed again — was
    # counted stale forever (GitHub issue #21).
    repo = AnalystRatingRepository(db)
    result = await db.execute(
        select(func.count(AnalystRating.id)).where(repo.in_scope())
    )
    total_ratings = result.scalar() or 0
    stale_ratings = await repo.count_stale_ratings()

    result = await db.execute(
        select(
            func.min(AnalystRating.last_updated),
            func.max(AnalystRating.last_updated)
        ).where(repo.in_scope())
    )
    oldest_update, newest_update = result.one()

    return {
        "total_ratings": total_ratings,
        "stale_ratings": stale_ratings,
        "fresh_ratings": total_ratings - stale_ratings,
        "oldest_update": utc_iso(oldest_update),
        "newest_update": utc_iso(newest_update),
    }


@router.get("/{security_id}")
async def get_analyst_rating(security_id: DbId, db: AsyncSession = Depends(get_db)):
    """
    Get analyst rating for a specific security.

    Args:
        security_id: ID of the security

    Returns:
        Analyst rating data or 404 if not found
    """
    # The company's rating, as the positions table and the overview show it (review item
    # DR8): its canonical listing's counts through `rating_of`. Yahoo's "no analysts cover
    # this" is no rating here as everywhere else, not "No Rating" over five zeros.
    canonical = await canonical_listings(db)
    rating_service = AnalystRatingService(db)
    rating = await rating_service.get_rating_for_security(
        canonical.get(security_id, security_id)
    )
    shown = rating_of(counts_of(rating))
    if shown is None:
        raise HTTPException(
            status_code=404,
            detail=f"No analyst rating found for security {security_id}"
        )

    return {
        "security_id": security_id,
        **shown,
        "total_ratings": rating.total_ratings,
        "last_updated": utc_iso(rating.last_updated),
    }
