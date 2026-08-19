import logging
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.auth import log_write_auth_state, write_auth_enabled, write_auth_middleware
from app.config import settings
from app.observability import request_id_middleware, unhandled_exception_handler
from app.rate_limit import rate_limit_middleware

# Third-party loggers that are chatty at INFO and say nothing we act on. yfinance in
# particular narrates every request, which would triple the volume of a market-data pass.
_NOISY_AT_INFO = ("yfinance", "peewee", "urllib3", "httpx", "httpcore", "asyncio", "apscheduler")


def _configure_logging() -> None:
    """
    Make `settings.log_level` mean something.

    It was read in exactly one place — `echo=settings.log_level == "DEBUG"` for SQLAlchemy
    — and nothing ever configured the logging module, so the root logger kept its default
    and Python's last-resort handler emitted **WARNING and above only**. Every
    `logger.info` in the app was silently discarded in production.

    That is worse than a missing feature, because the docs assume otherwise: CLAUDE.md
    tells you to grep the container log for a request id, and the scheduler's own
    `removing retired job` / `kept, next run:` lines — the evidence that misfire recovery
    and job pruning worked — are INFO. They could not be read, which is part of why a job
    store that never opened looked healthy for two days.

    Deliberately `force=True`: uvicorn installs its own handlers before the app is
    imported, and without it basicConfig is a no-op whenever any handler already exists.
    """
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    for name in _NOISY_AT_INFO:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))


_configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifecycle manager for the FastAPI application.
    Handles startup and shutdown events.
    """
    # No schema creation here, deliberately.
    #
    # This used to call `init_db()` -> `Base.metadata.create_all()` on every boot,
    # beside the `alembic upgrade head` that backend/Dockerfile's CMD already runs. It
    # was almost always a no-op, and the exception was the problem: a model added
    # without a migration got its table created silently, worked in production, and then
    # made the migration written for it later fail with "table already exists" at some
    # unrelated future deploy — a boot failure whose cause is months upstream of it.
    #
    # `tests/test_model_metadata.py` had already asserted, in prose, that "nothing in
    # production ever calls Base.metadata.create_all()". That is true now.
    # `tests/test_migrations.py` runs the chain and compares the result against
    # Base.metadata in both directions, which is what makes the safety net unnecessary
    # rather than merely absent. `init_db` itself stays in app/database.py for tests and
    # scripts that want a schema without a migration run.

    # Initialize default ticker mappings
    from app.database import AsyncSessionLocal
    from app.repositories.ticker_mapping_repository import TickerMappingRepository

    async with AsyncSessionLocal() as db:
        try:
            mapping_repo = TickerMappingRepository(db)
            count = await mapping_repo.initialize_default_mappings()
            if count > 0:
                await db.commit()
                logger.info(f"Initialized {count} default ticker mappings")
            else:
                logger.info("Default ticker mappings already exist")
        except Exception as e:
            logger.info(f"Warning: Could not initialize ticker mappings: {str(e)}")
            await db.rollback()

    # Seed the default base (display) currency
    from app.repositories.app_settings_repository import AppSettingsRepository

    async with AsyncSessionLocal() as db:
        try:
            settings_repo = AppSettingsRepository(db)
            if await settings_repo.ensure_default_base_currency():
                await db.commit()
                logger.info("Seeded default base currency (EUR)")
        except Exception as e:
            logger.info(f"Warning: Could not seed base currency: {str(e)}")
            await db.rollback()

    # Say plainly whether the write API is protected. An unprotected one that nobody
    # notices is exactly the failure app/auth.py exists to prevent.
    log_write_auth_state()

    # Start the scheduler for daily automatic syncs.
    #
    # Gated because starting it is not a neutral act: the jobs reach the live
    # IBKR Flex token and Yahoo, so a developer running uvicorn locally would
    # arm real syncs against a rate-limited, lockout-prone API. Defaults to on,
    # so production keeps its schedule; local .env sets SCHEDULER_ENABLED=false.
    from app.services.scheduler_service import ALL_SYNC_HOURS, get_scheduler
    scheduler = get_scheduler()
    if settings.scheduler_enabled:
        scheduler.start()
        # Built from the constant, not spelled out: this line still read
        # "08:00, 13:00, 15:00, 20:00, 22:00" for four days after those hours moved,
        # which is a log that actively misleads whoever is diagnosing a missed sync.
        slots = ", ".join(f"{h:02d}:00" for h in ALL_SYNC_HOURS)
        logger.info(f"Scheduler started - syncs at {slots} Europe/Berlin")
    else:
        # Logged loudly: a silently disabled scheduler looks exactly like a
        # healthy site whose data has quietly stopped moving.
        logger.warning(
            "Scheduler DISABLED (SCHEDULER_ENABLED=false) - no automatic IBKR, "
            "FX, market-data or dividend syncs will run in this process"
        )

    yield

    # Shutdown: Clean up resources
    logger.info("Application shutting down")
    # Safe when the scheduler was never started — shutdown() no-ops on a None
    # scheduler, which is exactly the disabled case.
    scheduler.shutdown()


# Create FastAPI application
app = FastAPI(
    title="IBKR Portfolio Analyzer API",
    description="API for tracking and analyzing Interactive Brokers portfolio with cost basis and market value",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# `add_middleware` builds the stack outermost-LAST, so these run in reverse of the
# order written: request id first (every log line and every response, including the
# ones the layers below reject, carries it), then the rate limit (cheap, and it should
# shed load before any real work), then write auth.
app.middleware("http")(write_auth_middleware)
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(request_id_middleware)

# Nothing escapes as an unlabelled 500 with a possibly-token-bearing message.
app.add_exception_handler(Exception, unhandled_exception_handler)


# Health check endpoint
@app.get("/")
async def root():
    return {
        "message": "IBKR Portfolio Analyzer API",
        "version": settings.app_version,
        "status": "running"
    }


@app.get("/health")
async def health_check(request: Request):
    """
    Liveness plus enough identity to answer "which build is live, and is it armed?"
    from a browser. Previously `{"status": "healthy"}` alone, so confirming a deploy
    had landed — or that the scheduler was actually running — needed ssh.

    Deliberately unauthenticated and un-throttled: the deploy script and any uptime
    check poll it, and it exposes no portfolio data.
    """
    from app.services.scheduler_service import get_scheduler

    return {
        "status": "healthy",
        "version": settings.app_version,
        "commit": settings.git_commit,
        "scheduler_enabled": settings.scheduler_enabled,
        # False means the job store fell back to memory, so a restart overlapping a
        # Berlin slot loses that sync. It is here rather than only in the logs because
        # the fallback re-registers every job, making `/api/scheduler/status` report a
        # fully-armed scheduler either way — the store was inert for two days behind
        # exactly that appearance.
        "scheduler_jobstore_persistent": get_scheduler().jobstore_persistent,
        "write_auth_enabled": write_auth_enabled(),
        "request_id": getattr(request.state, "request_id", None),
    }


# Import and include routers
from app.routers import sync, portfolio, market_data, analyst_ratings, allocation, scheduler, fundamentals, watchlist, dividends, tax, settings as settings_router

app.include_router(sync.router, prefix="/api/sync", tags=["sync"])
app.include_router(settings_router.router, prefix="/api/settings", tags=["settings"])
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
app.include_router(market_data.router, prefix="/api/market-data", tags=["market-data"])
app.include_router(analyst_ratings.router, prefix="/api/analyst-ratings", tags=["analyst-ratings"])
app.include_router(allocation.router, prefix="/api/allocation", tags=["allocation"])
app.include_router(scheduler.router, prefix="/api/scheduler", tags=["scheduler"])
app.include_router(fundamentals.router, prefix="/api/fundamentals", tags=["fundamentals"])
app.include_router(watchlist.router, prefix="/api/watchlist", tags=["watchlist"])
app.include_router(dividends.router, prefix="/api/dividends", tags=["dividends"])
app.include_router(tax.router, prefix="/api/tax", tags=["tax"])
# app.include_router(securities.router, prefix="/api/securities", tags=["securities"])
# app.include_router(taxlots.router, prefix="/api/taxlots", tags=["taxlots"])
