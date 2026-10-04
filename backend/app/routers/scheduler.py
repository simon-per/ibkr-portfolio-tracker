"""
Scheduler Router
API endpoints for managing the automated sync scheduler.
"""
import logging

from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redact import redact_secrets
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.scheduler_service import (
    SCHEDULED_JOB_TYPES,
    STOCK_JOB_GROUP,
    get_scheduler,
)
from app.single_flight import SYNC_PIPELINE, SyncBusy, is_running, single_flight

logger = logging.getLogger(__name__)

router = APIRouter()


async def _last_sync(scheduler, db: AsyncSession) -> Optional[Dict]:
    """
    Prefer the in-memory result, fall back to the persisted history.

    The in-memory value is lost on every container restart, and auto-deploy restarts on
    each push — so without this fallback the daily validator can see `last_sync: null`
    and wrongly conclude that no sync ran.

    The fallback reads the stock scheduler's own run types only (`SCHEDULED_JOB_TYPES`,
    a whitelist): the crypto sync records `crypto_sync` rows at the same slots, and the
    newest row of *any* type would make the dashboard's "Last sync" a crypto run.
    """
    if scheduler.last_sync_result:
        # In-memory job results carry raw step errors; redact like the stored rows.
        return redact_secrets(scheduler.last_sync_result)
    try:
        run = await SyncRunRepository(db).get_latest(sync_types=SCHEDULED_JOB_TYPES)
        return SyncRunRepository.to_dict(run) if run else None
    except Exception as e:  # history is a convenience; never fail the status call
        logger.warning(f"Could not read persisted sync history: {e}")
        return None


@router.post("/trigger", response_model=Dict)
async def trigger_sync_now():
    """
    Manually trigger the daily sync job immediately.

    This endpoint:
    1. Runs IBKR sync immediately
    2. Then runs market data sync
    3. Returns results from both operations

    Useful for testing or manual syncs outside of the scheduled times.

    Returns:
        Summary of sync operations
    """
    try:
        # Own cooldown on top of the pipeline gate: this endpoint is public and
        # runs the heaviest job there is (IBKR + 730d Yahoo + dividends).
        with single_flight("manual-trigger", cooldown_seconds=300):
            scheduler = get_scheduler()
            result = await scheduler.trigger_sync_now()
        return redact_secrets(result)

    except SyncBusy as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_seconds)},
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=redact_secrets(f"Failed to trigger sync: {str(e)}")
        )


@router.get("/status", response_model=Dict)
async def get_scheduler_status(db: AsyncSession = Depends(get_db)):
    """
    Get the current status of the scheduler, including all jobs and last sync result.

    Returns:
        Information about the scheduler, all scheduled jobs, and last sync result
    """
    try:
        scheduler = get_scheduler()

        # Whether the stock pipeline's gate (`SYNC_PIPELINE`) is held right now — by a
        # scheduled job or by a manual POST alike. In-process state, so this endpoint is
        # the only way to see it from outside: `deploy.sh` reads it to hold a restart
        # while a sync runs, since a sync killed mid-run re-runs from scratch and at the
        # IBKR slots that is a second Flex generation. The crypto gate is deliberately not
        # reported here (its status is admin-gated, docs/crypto.md). Both branches carry
        # it: a sync started by POST runs whether or not the scheduler is armed.
        sync_in_progress = is_running(SYNC_PIPELINE)

        if scheduler.scheduler is None:
            return {
                "status": "not_running",
                "message": "Scheduler is not running",
                "jobs": [],
                "sync_in_progress": sync_in_progress,
                "last_sync": await _last_sync(scheduler, db),
            }

        # The stock pipeline's jobs only. The crypto jobs share its slots and are
        # reported by the admin-gated `/api/crypto/status`; listing them here would put
        # a crypto job at `jobs[0]`, which the dashboard reads as its "Next:" run.
        jobs = []
        for job in scheduler.jobs_in_group(STOCK_JOB_GROUP):
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })

        return {
            "status": "running",
            "jobs": jobs,
            "sync_in_progress": sync_in_progress,
            "last_sync": await _last_sync(scheduler, db),
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=redact_secrets(f"Failed to get scheduler status: {str(e)}")
        )


@router.get("/history", response_model=Dict)
async def get_sync_history(limit: int = 20, db: AsyncSession = Depends(get_db)):
    """
    Recent sync attempts, newest first — the durable record of what ran and what broke.

    Leaves out the crypto runs (the sync and the rebuild CLI): this endpoint is public and the crypto book is
    not, and eight crypto rows a day would crowd the stock runs out of the default page.
    They are served by the admin-gated `/api/crypto/status`.
    """
    from app.services.crypto_service import PUBLIC_EXCLUDED_SYNC_TYPES

    try:
        repo = SyncRunRepository(db)
        runs = await repo.get_recent(limit, exclude_types=PUBLIC_EXCLUDED_SYNC_TYPES)
        return {"count": len(runs), "runs": [SyncRunRepository.to_dict(r) for r in runs]}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=redact_secrets(f"Failed to read sync history: {str(e)}")
        )
