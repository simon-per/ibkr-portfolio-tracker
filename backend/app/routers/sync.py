"""
Sync Router
Handles syncing data from IBKR Flex Query to local database.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict
from datetime import datetime
from app.clock import utcnow

from app.database import get_db
from app.redact import redact_secrets
from app.services.flex_generation import generation_already_spent, next_generation_opens_at
from app.services.ibkr_service import IBKRService
from app.single_flight import IBKR_SYNC_COOLDOWN_SECONDS, SYNC_PIPELINE, SyncBusy, single_flight
from app.services.sync_helper import ingest_flex_statement
from app.repositories.security_repository import SecurityRepository
from app.repositories.taxlot_repository import TaxLotRepository
from app.repositories.sync_run_repository import SyncRunRepository, utc_iso

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/ibkr", response_model=Dict)
async def sync_ibkr_data(force: bool = False, db: AsyncSession = Depends(get_db)):
    """
    Sync securities and tax lots from IBKR Flex Query.

    This endpoint:
    1. Fetches data from IBKR Flex Query API
    2. Extracts securities (stocks only - no dividends, cash, etc.)
    3. Extracts tax lots (purchase history)
    4. Converts cost basis to EUR
    5. Stores everything in the database

    Answers **200 with `status="skipped"`** — not an error — when IBKR has already
    generated today's statement. It issues roughly one per US-Eastern calendar day and
    refuses the rest with `Code=1001` at the SendRequest step, so the old behaviour was
    to spend a *failed generation* (what `Code=1025` counts) in order to show the user a
    red banner. Nothing is fetched and nothing is lost: the day's statement is already
    ingested.

    `force=true` overrides it, for the one case the recorded history shows really does
    allow a second generation in a day — editing the query definition in the IBKR
    portal. It costs lockout budget if the assumption is wrong, which is why it is not
    the default.

    Returns:
        Summary of synced data including counts
    """
    started_at = utcnow()

    # Checked *before* the pipeline gate, deliberately. Entering the gate bumps the
    # shared last-start clock every other route's cooldown reads, so a button press that
    # does no work must not 429 a real sync behind it.
    if not force:
        spent = await generation_already_spent(db)
        if spent is not None:
            next_at = next_generation_opens_at()
            return {
                "status": "skipped",
                "reason": "already_generated_today",
                "message": (
                    "IBKR has already generated today's statement, so there is nothing "
                    "new to fetch. It issues one per US-Eastern day; the next becomes "
                    "available at the time below."
                ),
                "last_success_at": utc_iso(spent),
                "next_attempt_after": utc_iso(next_at),
            }

    try:
        # Shared pipeline gate + cooldown: this route is public and one Flex
        # round is several requests against a 10/min token budget.
        with single_flight(SYNC_PIPELINE, cooldown_seconds=IBKR_SYNC_COOLDOWN_SECONDS):
            return await _sync_ibkr_locked(db, started_at)
    except SyncBusy as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_seconds)},
        )
    except Exception as e:
        await db.rollback()
        await SyncRunRepository(db).record(
            sync_type="ibkr", status="error", message=str(e), started_at=started_at,
        )
        raise HTTPException(
            status_code=500,
            detail=redact_secrets(f"Failed to sync IBKR data: {str(e)}")
        )


async def _sync_ibkr_locked(db: AsyncSession, started_at: datetime) -> Dict:
    """The sync body, run while holding the pipeline gate. Errors are handled by the caller."""
    # Step 1: Fetch data from IBKR
    flex_data = await IBKRService().fetch_flex_data()

    # Steps 2-6 are shared with the scheduled jobs and the offline XML ingest, so
    # every path reconciles in exactly the same order.
    ingested = await ingest_flex_statement(db, flex_data)

    # Commit transaction
    await db.commit()

    warnings = ingested.pop("warnings", [])
    result = {
        "status": "success",
        "message": "Successfully synced data from IBKR",
        **ingested,
    }

    # Surface skipped currencies plus any Flex XML schema drift the sanitizer had
    # to work around, so it's visible in the API/UI and not only in container logs.
    if warnings:
        result["warnings"] = warnings

    # Persist the attempt so it survives container restarts (auto-deploy restarts on
    # every push). Best-effort: never turn a good sync into a failure.
    await SyncRunRepository(db).record(
        sync_type="ibkr", status="success", message=result["message"],
        details={k: v for k, v in result.items() if k not in ("message", "warnings")},
        warnings=warnings or None, started_at=started_at,
    )

    return result


@router.get("/status")
async def get_sync_status(db: AsyncSession = Depends(get_db)):
    """
    Get current sync status - count of securities and taxlots in database.
    """
    security_repo = SecurityRepository(db)
    taxlot_repo = TaxLotRepository(db)

    securities = await security_repo.get_all(limit=1000)
    open_taxlots = await taxlot_repo.get_open_taxlots()

    total_cost_basis_eur = sum(
        lot.cost_basis_eur for lot in open_taxlots
    )

    return {
        "securities_count": len(securities),
        "open_taxlots_count": len(open_taxlots),
        "total_cost_basis_eur": float(total_cost_basis_eur),
    }
