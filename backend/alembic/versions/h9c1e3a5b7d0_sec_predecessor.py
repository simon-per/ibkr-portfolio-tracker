"""sec_companies.predecessor_cik

Revision ID: h9c1e3a5b7d0
Revises: g8b0d2f4a6c9
Create Date: 2026-11-19 22:30:00.000000

The filer a company succeeded (docs/company-financials.md, *Successor filers*): ExxonMobil
Holdings, a new registrant over Exxon Mobil Corp since 2026, had two quarters and no history.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "h9c1e3a5b7d0"
down_revision: Union[str, None] = "g8b0d2f4a6c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sec_companies", sa.Column("predecessor_cik", sa.Integer(), nullable=True))
    op.add_column(
        "sec_companies", sa.Column("predecessor_checked_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("sec_companies") as batch:
        batch.drop_column("predecessor_checked_at")
        batch.drop_column("predecessor_cik")
