from sqlalchemy import String, Text, DateTime, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional, Any

from app.database import Base


class SyncRun(Base):
    """
    One recorded sync attempt — IBKR, market data or a full run.

    Exists because ``SchedulerService.last_sync_result`` lives only in memory, so it
    vanishes whenever the container restarts. Auto-deploy restarts on every push, which
    made the daily `ibkr-sync-validator` routine unable to tell whether the morning sync
    had run at all. Persisting each attempt makes the history survive restarts and gives
    the UI and the validator one durable place to look.

    Written on a best-effort basis: recording must never fail a sync that otherwise
    succeeded, nor mask the original error.
    """
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # 'ibkr' (manual endpoint) | 'full_sync' | 'ibkr_sync' | 'market_data_only' | the CLIs'
    # own types | 'crypto_sync' (docs/crypto.md: type, status, reason and message only, and
    # left out of the public /api/scheduler endpoints)
    sync_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # success | error
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Counts and per-step results, so a run can be understood after the fact.
    details: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # Flex XML schema drift the sanitizer worked around, unsupported currencies, etc.
    warnings: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_sync_runs_type_finished", "sync_type", "finished_at"),
    )

    def __repr__(self) -> str:
        return f"<SyncRun({self.sync_type} {self.status} at {self.finished_at})>"
