from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

# Get the backend directory (parent of app/)
# In Docker: /app/app/config.py → /app/.env
# Locally: backend/app/config.py → backend/.env
BACKEND_ROOT = Path(__file__).parent.parent
ENV_FILE = BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    # IBKR Configuration
    ibkr_token: str
    ibkr_query_id: str

    # Alpha Vantage Configuration
    alpha_vantage_api_key: str = ""  # Optional for initial setup

    # Contact address advertised in the User-Agent when the look-through feature calls
    # OpenFIGI, GLEIF or an ETF issuer's holdings endpoint. Those are third-party sites
    # used as a guest rather than an API we are a customer of, and identifying the client
    # honestly is the price of that. Empty simply omits it — nothing breaks.
    # A setting rather than a literal because this repository is public and an operator's
    # email does not belong in it.
    lookthrough_contact_email: str = ""

    # OpenFIGI's free API key, sent as `X-OPENFIGI-APIKEY`. Optional and empty by default —
    # the keyless tier works and is what the feature shipped on — but it lifts the batch from
    # 10 mapping jobs per request to 100 and the rate from 25 requests/minute to 250, i.e.
    # from ~250 identifiers a minute to ~25,000. That is the difference between resolving the
    # few hundred ISINs held directly and resolving the thousands inside the funds, so the key
    # has to be in place *before* `IDENTITY_MAX_ISINS` is worth raising.
    openfigi_api_key: str = ""

    # Database Configuration
    database_url: str = "sqlite+aiosqlite:///./portfolio.db"

    # CORS Configuration (comma-separated string to avoid Pydantic JSON-parsing issues with List from env vars)
    cors_origins: str = "http://localhost:5173,http://localhost:5174,http://localhost:3000"

    # Logging
    log_level: str = "INFO"

    # Whether to arm the APScheduler jobs on startup. Defaults to True so
    # production is unaffected; set SCHEDULER_ENABLED=false for local runs.
    # Without it, merely starting uvicorn on a dev machine arms every job in
    # ALL_SYNC_HOURS (scheduler_service) against the real IBKR token in .env and
    # against Yahoo — the two things this project must never do casually (see the
    # two rules at the top of CLAUDE.md). Deliberately not listing the hours here:
    # the copy that used to sit in this comment went stale the day they changed.
    scheduler_enabled: bool = True

    # Shared secret required on POST/PUT/PATCH/DELETE under /api/ (see app/auth.py).
    # Empty (the default) disables the check entirely, so an existing deployment keeps
    # working until this is set deliberately — enabling it is a decision, not something
    # a deploy does to a running site. Startup warns loudly while it is unset.
    api_admin_token: str = ""

    # Requests per minute per client IP across /api/ (see app/rate_limit.py). Generous:
    # one dashboard load issues a dozen or so. 0 disables the limiter.
    rate_limit_per_minute: int = 120

    # Identifies the running build on /health, so "did my deploy land" is answerable
    # from the browser. Set by deploy.sh / ops/auto-deploy.sh from the deployed sha.
    git_commit: str = "unknown"
    app_version: str = "1.0.0"

    # Where APScheduler persists its jobs (see scheduler_service). A SEPARATE file from
    # portfolio.db: APScheduler's store is synchronous SQLAlchemy while the app is
    # aiosqlite in WAL mode, and pointing both at one file invites lock contention on
    # the database that holds the actual portfolio.
    # Inside a directory, because that directory is what docker-compose bind-mounts —
    # see the comment there. Pointing the mount at the .db file itself made Docker
    # create it as a directory and sqlite could never open it.
    scheduler_jobstore_url: str = "sqlite:///./scheduler-data/scheduler_jobs.db"

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False
    )

    @property
    def cors_origins_list(self) -> list:
        return [origin.strip() for origin in self.cors_origins.split(",")]


# Create a global settings instance
settings = Settings()
