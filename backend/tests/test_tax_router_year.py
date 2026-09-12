"""
The tax router's year ceiling was `date.today().year` evaluated at import. On
1 January the current year answered 422 and the default served the prior year until
the container happened to restart — a silent yearly outage on the one report that is
read once a year, in January.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.routers import tax as tax_router


def test_the_ceiling_follows_the_clock_not_the_process_start(monkeypatch):
    monkeypatch.setattr(tax_router, "utcnow", lambda: datetime(2027, 1, 1, 0, 30))

    assert tax_router._resolve_year(None) == 2027   # default is the current year
    assert tax_router._resolve_year(2027) == 2027   # and it is accepted on the day
    assert tax_router._resolve_year(2026) == 2026

    with pytest.raises(HTTPException) as exc:
        tax_router._resolve_year(2028)
    assert exc.value.status_code == 422


def test_no_year_is_frozen_at_import():
    """A module-level year is the whole bug; keep the constant from coming back."""
    assert not hasattr(tax_router, "_MAX_YEAR")
    assert not hasattr(tax_router, "date"), "the router should not need datetime.date at all"
