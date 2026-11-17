"""
`c4e6a8b0d2f4` adds `companies` (another regulator's company, a surrogate id) and
`company_financials.company_id` beside the CIK (docs/company-financials.md, *Markets beyond
the SEC*). The SEC rows must come through untouched, a row must name exactly one company, and
the way back drops only the other regulators' rows.
"""

import sqlite3

import pytest
from alembic import command

from tests.test_migrations import _alembic_config

BEFORE = "b3d5f7a9c1e2"
FIGURE = (
    "INSERT INTO company_financials (cik, period_end, fiscal_year, fiscal_period,"
    " metric, value, unit, source_concept, filed, fetched_at)"
    " VALUES (?, '2025-01-26', 2025, 'FY', ?, ?, 'USD', 'Revenues', '2025-02-26',"
    " '2026-11-16 05:00:00')"
)
OTHER = (
    "INSERT INTO company_financials (cik, company_id, period_end, fiscal_year, fiscal_period,"
    " metric, value, unit, source_concept, filed, fetched_at)"
    " VALUES (?, ?, '2024-12-31', 2024, 'FY', 'revenue', 66193.0, 'KRW',"
    " 'dart:ifrs-full_Revenue', '2025-03-19', '2026-11-17 05:00:00')"
)


def test_the_sec_rows_stay_and_a_row_names_one_company(tmp_path, monkeypatch):
    from app.config import settings

    db_path = tmp_path / "before.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, BEFORE)

    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO sec_companies (cik, ticker) VALUES (1045810, 'NVDA')")
    con.executemany(
        FIGURE, [(1045810, "revenue", 130.0), (1045810, "net_income", 72.0)]
    )
    con.commit()
    con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    assert con.execute(
        "SELECT cik, company_id, metric, value FROM company_financials ORDER BY metric"
    ).fetchall() == [
        (1045810, None, "net_income", 72.0),
        (1045810, None, "revenue", 130.0),
    ]
    con.execute(
        "INSERT INTO companies (id, scheme, native_id, ticker) VALUES (1, 'dart', '00164779', '000660')"
    )
    con.execute(OTHER, (None, 1))
    con.commit()
    # Neither company, or both: refused.
    for owner in ((None, None), (1045810, 1)):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(OTHER, owner)
    # One period per metric per company, on the new key as on the CIK.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(OTHER, (None, 1))
    con.rollback()
    con.close()

    # And back: the SEC rows stay, the other regulator's go with its table.
    command.downgrade(cfg, BEFORE)
    con = sqlite3.connect(db_path)
    assert con.execute(
        "SELECT cik, metric FROM company_financials ORDER BY metric"
    ).fetchall() == [(1045810, "net_income"), (1045810, "revenue")]
    columns = {r[1] for r in con.execute("PRAGMA table_info(company_financials)")}
    assert "company_id" not in columns
    assert (
        con.execute(
            "SELECT name FROM sqlite_master WHERE name = 'companies'"
        ).fetchall()
        == []
    )
    con.close()
