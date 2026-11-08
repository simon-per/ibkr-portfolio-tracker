"""sec_company_filed_splits

Revision ID: a8d0f2b4c6e7
Revises: f6b8d0e2a4c5
Create Date: 2026-11-08 15:00:00.000000

The splits a company's filings restated history for (`normaliser.filed_splits`), as JSON,
written with its rows. HON's 1:2 at its 2026 Aerospace separation restated every earlier
EPS while Yahoo's closes were never adjusted, so every earlier P/E was half (review item
CO1). NULL on arrival: the closes stay as they are until the company's next facts fetch
writes its splits, and nothing is requested.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a8d0f2b4c6e7"
down_revision: Union[str, None] = "f6b8d0e2a4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain ADD COLUMN: SQLite does it in place, with no table rebuild.
    op.add_column("sec_companies", sa.Column("filed_splits", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sec_companies") as batch:
        batch.drop_column("filed_splits")
