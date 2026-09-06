from sqlalchemy import String, Numeric, Date
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from app.accounts import IBKR
from app.database import Base

# flow_type values. Deposits are selected by an explicit whitelist rather than by
# excluding transfers, so a new transfer-ish type can never silently leak into the
# "money added" series.
DEPOSIT_WITHDRAW = "DEPOSITWITHDRAW"
TRANSFER = "TRANSFER"              # direction unknown
TRANSFER_IN = "TRANSFER_IN"
TRANSFER_OUT = "TRANSFER_OUT"
TRANSFER_TYPES = (TRANSFER, TRANSFER_IN, TRANSFER_OUT)

# Costs and income a broker statement does not itemise but a Pillar 3a export does.
# They move cash and are emphatically not money added, which the whitelist above
# already guarantees without naming them. IBKR's equivalents remain invisible — the
# documented ~-250 CHF drift in the derived balance is exactly these.
#
# INCOME covers every inbound distribution (dividend, interest, liquidation
# proceeds) and exists so a tax-exempt account's income has somewhere to go that is
# *not* `dividend_payments`. That is an exclusion achieved by not writing the row,
# rather than by a filter three readers have to remember: the era splice, the
# forecast and the DA-1 reclaim all stay IBKR-only for free.
FEE = "FEE"
INCOME = "INCOME"


class CashFlow(Base):
    """
    External cash moving into or out of the account, from the Flex Query
    <CashTransactions> section (``type="Deposits & Withdrawals"``).

    This exists because tax lots cannot answer "how much money did I add".
    A purchase funded by selling something else is indistinguishable, in the lot
    data, from a purchase funded by new money — so summing lot cost basis counts
    rotated capital twice, and netting closures out instead debits a window for a
    purchase made before it. A deposit has a single leg, so neither error applies.

    Idempotent on ``ib_key`` (IBKR transactionID). ``amount`` keeps IBKR's sign:
    deposits positive, withdrawals negative — the one enum value covers both
    directions, so the sign is the only thing distinguishing them.
    """
    __tablename__ = "cash_flows"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    ib_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    flow_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # DEPOSITWITHDRAW | TRANSFER_IN | TRANSFER_OUT | TRANSFER. A string rather than a
    # bool so the other non-dividend cash types (broker interest, fees) can be added
    # later without a migration.
    flow_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # See app/accounts.py. Pillar 3a cash is locked, so it is derived and reported
    # separately rather than summed into the balance a purchase could draw on.
    account: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=IBKR, default=IBKR, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # signed, original currency
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    # Pre-converted at flow_date, matching taxlots.cost_basis_eur / dividend_payments.*_eur:
    # the pipeline computes in EUR and projects into the base currency at read time.
    amount_eur: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<CashFlow(ib_key={self.ib_key}, {self.flow_type} "
            f"{self.amount} {self.currency} on {self.flow_date})>"
        )
