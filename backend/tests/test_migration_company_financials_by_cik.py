"""
`e5f7a9b1c3d6` moves the Company tab's figures from one set per security to one per SEC
filer (docs/company-financials.md). Run on rows shaped like production's: GOOG and GOOGL
carried the same figures twice, and the pass's stamps lived on `security_ciks`.
"""

import sqlite3

from alembic import command

from tests.test_migrations import _alembic_config

BEFORE = "d1e3f5a7b9c2"
FIGURE = (
    "INSERT INTO company_financials (security_id, period_end, fiscal_year, fiscal_period,"
    " metric, value, unit, source_concept, filed, fetched_at)"
    " VALUES (?, '2025-01-26', 2025, 'FY', ?, ?, 'USD', 'Revenues', '2025-02-26',"
    " '2026-10-25 05:00:00')"
)


def test_the_rows_move_to_their_company_once_and_the_stamps_with_them(
    tmp_path, monkeypatch
):
    from app.config import settings

    db_path = tmp_path / "before.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, BEFORE)

    con = sqlite3.connect(db_path)
    con.executemany(
        "INSERT INTO security_ciks (security_id, cik, sec_ticker, cik_checked_at,"
        " facts_checked_at, facts_miss_count, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1045810, "NVDA", "2026-10-25", None, None, "2026-10-25 05:00:00"),
            (2, 1652044, "GOOGL", "2026-10-25", None, None, "2026-10-25 05:00:00"),
            (3, 1652044, "GOOG", "2026-10-25", None, None, "2026-10-25 05:00:00"),
            (4, 937966, "ASML", "2026-10-25", "2026-10-25 05:00:00", 2, None),
            (5, None, "ABEA", "2026-10-25", None, None, None),
        ],
    )
    con.executemany(
        FIGURE,
        [
            (1, "revenue", 130.0),
            (1, "net_income", 72.0),
            (2, "revenue", 350.0),
            (3, "revenue", 350.0),
        ],
    )
    con.commit()
    con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT cik, metric, value FROM company_financials ORDER BY cik, metric"
    ).fetchall()
    assert rows == [
        (1045810, "net_income", 72.0),
        (1045810, "revenue", 130.0),
        (1652044, "revenue", 350.0),  # GOOG and GOOGL: one copy
    ]
    companies = con.execute(
        "SELECT cik, ticker, fetched_at, facts_miss_count FROM sec_companies ORDER BY cik"
    ).fetchall()
    assert companies == [
        (937966, "ASML", None, 2),  # the miss keeps its back-off
        (1045810, "NVDA", "2026-10-25 05:00:00", None),
        (1652044, "GOOG", "2026-10-25 05:00:00", None),
    ]
    columns = {r[1] for r in con.execute("PRAGMA table_info(security_ciks)")}
    assert columns == {"security_id", "cik", "sec_ticker", "cik_checked_at"}
    con.close()

    # And back: the figures return under every listing of their company.
    command.downgrade(cfg, BEFORE)
    con = sqlite3.connect(db_path)
    back = con.execute(
        "SELECT security_id, metric FROM company_financials ORDER BY security_id, metric"
    ).fetchall()
    assert back == [(1, "net_income"), (1, "revenue"), (2, "revenue"), (3, "revenue")]
    con.close()
