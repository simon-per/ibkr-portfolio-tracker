"""drop_benchmark_timeline_cache

Revision ID: u4d1f8a5b9c0
Revises: t3c0e7f4a8b9
Create Date: 2026-09-08 20:00:00.000000

The table cached the *inception-anchored* benchmark series, one row per (benchmark,
date). It was sound only given a fixed contribution basis, so three call sites cleared
it — after every Flex ingest, after every finpension import, and daily for the trailing
week — and since 2026-09-07 the chart asks for `anchor=window`, which never read or
wrote it. A cache nothing reads is a cache whose staleness nobody notices; the walk it
saved is O(days) over preloaded prices, and the expensive steps (the provider fetches)
never went through it. Retired rather than left half-alive.

The downgrade recreates the empty table so `a3b7c1d2e4f5`'s schema is restored; the
service that would read it is gone, so it stays empty either way.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'u4d1f8a5b9c0'
down_revision: Union[str, None] = 't3c0e7f4a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_benchmark_cache_key_date', table_name='benchmark_timeline_cache')
    op.drop_index(op.f('ix_benchmark_timeline_cache_date'), table_name='benchmark_timeline_cache')
    op.drop_index(op.f('ix_benchmark_timeline_cache_benchmark_key'), table_name='benchmark_timeline_cache')
    op.drop_index(op.f('ix_benchmark_timeline_cache_id'), table_name='benchmark_timeline_cache')
    op.drop_table('benchmark_timeline_cache')


def downgrade() -> None:
    op.create_table('benchmark_timeline_cache',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('benchmark_key', sa.String(length=50), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('benchmark_value_eur', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('cost_basis_eur', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('gain_loss_eur', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('gain_loss_percent', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('benchmark_key', 'date', name='uix_benchmark_cache_key_date')
    )
    op.create_index(op.f('ix_benchmark_timeline_cache_id'), 'benchmark_timeline_cache', ['id'], unique=False)
    op.create_index(op.f('ix_benchmark_timeline_cache_benchmark_key'), 'benchmark_timeline_cache', ['benchmark_key'], unique=False)
    op.create_index(op.f('ix_benchmark_timeline_cache_date'), 'benchmark_timeline_cache', ['date'], unique=False)
    op.create_index('ix_benchmark_cache_key_date', 'benchmark_timeline_cache', ['benchmark_key', 'date'], unique=False)
