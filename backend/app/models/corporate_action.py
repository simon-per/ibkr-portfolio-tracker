from sqlalchemy import String, ForeignKey, Numeric, Date, Text, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from app.database import Base


class CorporateAction(Base):
    """
    A corporate action affecting a holding (split, reverse split, spinoff,
    merger, symbol change, stock dividend...), parsed from the Flex Query
    <CorporateActions> section. Lets reconciliation classify quantity changes
    deterministically instead of inferring them from a cost-conservation
    heuristic.

    ``action_type`` stores the ibflex Reorg enum name (e.g. FORWARDSPLIT,
    REVERSESPLIT, SPINOFF, MERGER, ISSUECHANGE). Idempotent on ``ib_key``
    (IBKR transactionID).
    """
    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ib_key: Mapped[str] = mapped_column(String(64), nullable=False)
    conid: Mapped[str] = mapped_column(String(32), nullable=False)
    security_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action_date: Mapped[date] = mapped_column(Date, nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)  # Reorg enum name
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)  # change in quantity
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    proceeds: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # actionDescription
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    # Named as migration i2d9f6a3b5c7 created them (`ix_corp_actions_*`, and a unique
    # *constraint* on ib_key rather than a unique index), not as `index=True` would name
    # them: `alembic check` in tests/test_migrations.py fails on any difference.
    __table_args__ = (
        UniqueConstraint("ib_key", name="uq_corp_actions_ib_key"),
        Index("ix_corp_actions_conid", "conid"),
        Index("ix_corp_actions_security_id", "security_id"),
        Index("ix_corp_actions_action_date", "action_date"),
        Index("ix_corp_actions_conid_date", "conid", "action_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<CorporateAction(ib_key={self.ib_key}, conid={self.conid}, "
            f"type={self.action_type}, qty={self.quantity}, on={self.action_date})>"
        )
