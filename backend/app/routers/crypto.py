"""
`/api/crypto/*` — the crypto book (docs/crypto.md).

**Every route here needs the admin key, reads included, and fails closed** —
`app/auth.py` gates the prefix for every method but OPTIONS and refuses outright when no
`API_ADMIN_TOKEN` is configured. The stock book's reads are public; the owner chose not
to publish crypto balances the same way.

The GETs are database-only: no CoinStats, CoinGecko or Frankfurter call, no write.
Only `POST /sync` reaches CoinStats and CoinGecko. Every response is marked `Cache-Control: private,
no-store`, so no proxy or shared browser cache keeps a copy of a private balance.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redact import redact_secrets
from app.schemas.crypto import (
    CryptoHistoryResponse,
    CryptoPortfolioResponse,
    CryptoStatusResponse,
    CryptoSyncResponse,
)
from app.services.coinstats_client import is_configured
from app.services.crypto_service import (
    CRYPTO_GATE,
    CRYPTO_MANUAL_GATE,
    MANUAL_COOLDOWN_SECONDS,
    CryptoService,
    CryptoSyncService,
)
from app.single_flight import SyncBusy, cooldown_remaining, is_running, single_flight

logger = logging.getLogger(__name__)


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(dependencies=[Depends(_private)])


@router.get("/portfolio", response_model=CryptoPortfolioResponse)
async def get_crypto_portfolio(db: AsyncSession = Depends(get_db)):
    """The newest snapshot: totals and holdings, both from the same sync."""
    return await CryptoService(db).portfolio()


@router.get("/history", response_model=CryptoHistoryResponse)
async def get_crypto_history(db: AsyncSession = Depends(get_db)):
    """The book's daily value and P&L from 2026-01-01, computed from the daily holdings
    and CoinGecko's prices."""
    return await CryptoService(db).history()


@router.get("/status", response_model=CryptoStatusResponse)
async def get_crypto_status(db: AsyncSession = Depends(get_db)):
    """The last crypto run, the next scheduled one and the credit balance last seen."""
    from app.services.scheduler_service import CRYPTO_JOB_GROUP, get_scheduler

    return await CryptoService(db).status(
        next_run=get_scheduler().next_run_time(CRYPTO_JOB_GROUP),
        sync_in_progress=is_running(CRYPTO_GATE),
        retry_after_seconds=cooldown_remaining(CRYPTO_MANUAL_GATE, MANUAL_COOLDOWN_SECONDS),
    )


@router.post("/sync", response_model=CryptoSyncResponse)
async def sync_crypto():
    """
    Run a crypto sync now: CoinStats' snapshot and CoinGecko's prices. Synchronous — a pass is a handful of requests.

    Configured-ness is checked first, so a misconfigured server answers 409 without
    spending the button's cooldown. A scheduled run already in flight answers 429 the
    same way; only a run that actually starts consumes the cooldown.
    """
    if not is_configured():
        raise HTTPException(
            status_code=409,
            detail="CoinStats is not configured: set COIN_STATS_API_KEY and "
                   "COIN_STATS_SHARE_TOKEN in backend/.env.",
        )
    if is_running(CRYPTO_GATE):
        raise HTTPException(
            status_code=429, detail="A crypto sync is already running.",
            headers={"Retry-After": "30"},
        )
    try:
        with single_flight(CRYPTO_MANUAL_GATE, cooldown_seconds=MANUAL_COOLDOWN_SECONDS):
            with single_flight(CRYPTO_GATE):
                result = await CryptoSyncService().sync()
    except SyncBusy as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_seconds)},
        )
    if result is None:  # the configuration went away between the check and the run
        raise HTTPException(status_code=409, detail="CoinStats is not configured.")
    return redact_secrets(result)
