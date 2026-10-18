"""
A Flex generation IBKR served is spent the moment it is served, and a refusal is remembered.

GitHub issue #55, both halves verified by an offline repro before the fix:

- The spend was recorded only by the run's final `success` row — after the ingest, and for
  `full_sync` after the whole long tail — so a served statement whose ingest failed (or a
  restart in that tail) left the ET day looking unspent, and the 00:00 slot and every manual
  press sent a second SendRequest: a `1001` at the request step, which `Code=1025` counts.
- The manual sync kept no memory of a `1025` lockout or a request-step `1001`: only its
  120 s start cooldown fenced it, so a press two minutes later asked the locked token again.

Fully offline. The Flex transport is stubbed below `fetch_flex_data`, so the real
`fetch_flex_data` — and the recording it now does — runs; any HTTP send fails the test.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from ibflex.client import ResponseCodeError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.routers.sync as sync_router
import app.single_flight as sf
from app.clock import utcnow
from app.database import Base
from app.models.sync_run import SyncRun
from app.routers.scheduler import _last_sync
from app.services import flex_generation as fg
from app.services import ibkr_service as ibkr_module
from app.services import scheduler_service as ss
from app.services.ibkr_service import IBKRService

XML = b"<FlexQueryResponse queryName='test' type='AF'><FlexStatements count='1'/></FlexQueryResponse>"

# 2026-08-08 12:00 UTC is 08:00 ET: mid-morning in New York, far from an ET day boundary.
NOW = datetime(2026, 8, 8, 12, 0, 0)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Every outbound HTTP send fails the test: nothing here may reach IBKR or anything else."""
    import httpx
    import requests

    attempts = []

    def _refuse(*a, **k):
        attempts.append(repr(a[:2]))
        raise ConnectionRefusedError("network disabled in this test")

    async def _arefuse(*a, **k):
        attempts.append(repr(a[:2]))
        raise httpx.ConnectError("network disabled in this test")

    monkeypatch.setattr(httpx.AsyncClient, "send", _arefuse)
    monkeypatch.setattr(requests.Session, "send", _refuse)
    yield
    assert attempts == []


@pytest.fixture(autouse=True)
def _fresh_gate():
    sf._running.clear()
    sf._last_start.clear()
    yield
    sf._running.clear()
    sf._last_start.clear()


@pytest_asyncio.fixture
async def maker(monkeypatch, tmp_path):
    """One in-memory database behind every session factory the paths under test open."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ss, "AsyncSessionLocal", mk)
    monkeypatch.setattr(fg, "AsyncSessionLocal", mk)
    monkeypatch.setattr(fg, "flex_statement_dir", lambda: tmp_path / fg.FLEX_STATEMENT_DIRNAME)
    # A trading ET day, so the no_new_trading_day skip cannot mask what is under test.
    monkeypatch.setattr(ss, "generation_brings_new_trading_day", lambda *a, **k: True)
    yield mk
    await engine.dispose()


@pytest.fixture
def served(monkeypatch):
    """IBKR serves a statement on every SendRequest; counts them. Parsing is stubbed."""
    calls = {"send_request": 0}

    def _download(self, deadline_seconds):
        calls["send_request"] += 1
        return XML

    monkeypatch.setattr(IBKRService, "_download_statement", _download)
    monkeypatch.setattr(IBKRService, "parse_flex_xml", lambda self, xml: {
        "statement": object(), "account_id": "U0", "from_date": None, "to_date": None,
        "flex_warnings": [], "flex_notes": [],
    })
    return calls


def _refused(monkeypatch, code, msg="refused"):
    """SendRequest answers with IBKR error `code`; counts the requests."""
    calls = {"send_request": 0}

    def _send(token, query_id):
        calls["send_request"] += 1
        raise ResponseCodeError(code, msg)

    monkeypatch.setattr(ibkr_module, "send_flex_request", _send)
    return calls


async def _refusal_message(monkeypatch, code):
    """The exact message fetch_flex_data raises for a refusal — never a copy of its text."""
    _refused(monkeypatch, code)
    with pytest.raises(RuntimeError) as e:
        await IBKRService(token="t", query_id="q").fetch_flex_data()
    return str(e.value)


def _scheduler(monkeypatch):
    svc = ss.SchedulerService()

    async def _fx(*a, **k):
        return {"status": "success"}

    monkeypatch.setattr(svc, "sync_exchange_rates", _fx)
    return svc


async def _rows(mk):
    async with mk() as db:
        return (await db.execute(
            select(SyncRun.sync_type, SyncRun.status).order_by(SyncRun.id)
        )).all()


async def _add(mk, sync_type, status, at, message=None, details=None):
    async with mk() as db:
        db.add(SyncRun(
            sync_type=sync_type, status=status, finished_at=at, message=message,
            details=details,
        ))
        await db.commit()


def _cool_down():
    sf._last_start[sf.SYNC_PIPELINE] -= sf.IBKR_SYNC_COOLDOWN_SECONDS + 1


# ── the spend is recorded the moment IBKR serves ──────────────────────────────

@pytest.mark.asyncio
async def test_a_served_generation_whose_ingest_fails_is_not_requested_again(
    maker, served, monkeypatch, tmp_path
):
    """The first repro: 18:00 serves, the ingest fails, 00:00 must not ask again."""
    async def _ingest_raises(db, flex_data):
        raise RuntimeError("ingest refused (the wipe guard, a parse error, FX)")

    monkeypatch.setattr(ss, "ingest_flex_statement", _ingest_raises)
    svc = _scheduler(monkeypatch)

    first = await svc._ibkr_only_sync_locked()
    assert first["ibkr_result"]["status"] == "error"
    assert served["send_request"] == 1
    async with maker() as db:
        assert await fg.generation_already_spent(db) is not None

    second = await svc._ibkr_only_sync_locked()
    assert second["ibkr_result"]["status"] == "skipped"
    assert second["reason"] == "already_generated_today"
    assert served["send_request"] == 1

    # And the statement IBKR served is on disk for app/cli/ingest_flex_xml.py.
    kept = list((tmp_path / fg.FLEX_STATEMENT_DIRNAME).glob("flex-*.xml"))
    assert [p.read_bytes() for p in kept] == [XML]
    async with maker() as db:
        row = (await db.execute(
            select(SyncRun).where(SyncRun.sync_type == fg.FLEX_GENERATION_SYNC_TYPE)
        )).scalar_one()
    assert row.status == "success"
    assert row.details == {"bytes": len(XML), "xml_file": kept[0].name}


@pytest.mark.asyncio
async def test_a_full_sync_stopped_after_its_ibkr_step_has_still_spent_the_day(
    maker, served, monkeypatch
):
    """The second repro: a restart in full_sync's long tail used to leave no row at all."""
    async def _ok(db, flex_data):
        return {"securities_synced": 1, "taxlots_synced": 1, "warnings": []}

    async def _stopped(*a, **k):
        raise asyncio.CancelledError()  # the container stops during the tail

    monkeypatch.setattr(ss, "ingest_flex_statement", _ok)
    svc = ss.SchedulerService()
    monkeypatch.setattr(svc, "sync_exchange_rates", _stopped)

    with pytest.raises(asyncio.CancelledError):
        await svc._full_sync_job_locked()

    assert await _rows(maker) == [(fg.FLEX_GENERATION_SYNC_TYPE, "success")]
    async with maker() as db:
        assert await fg.generation_already_spent(db) is not None

    retry = _scheduler(monkeypatch)
    result = await retry._ibkr_only_sync_locked()
    assert result["reason"] == "already_generated_today"
    assert served["send_request"] == 1


@pytest.mark.asyncio
async def test_a_manual_press_after_a_failed_ingest_is_skipped_not_re_requested(
    maker, served, monkeypatch
):
    async def _ingest_raises(db, flex_data):
        raise RuntimeError("ingest refused")

    monkeypatch.setattr(sync_router, "ingest_flex_statement", _ingest_raises)

    async with maker() as db:
        with pytest.raises(HTTPException) as first:
            await sync_router.sync_ibkr_data(force=False, db=db)
        assert first.value.status_code == 500

        _cool_down()
        second = await sync_router.sync_ibkr_data(force=False, db=db)

    assert second["status"] == "skipped"
    assert second["reason"] == "already_generated_today"
    assert served["send_request"] == 1


@pytest.mark.asyncio
async def test_recording_the_spend_never_costs_the_statement(maker, served, monkeypatch):
    """Best effort: an unwritable history and an unwritable disk still hand back the data."""
    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("database is locked")

        async def __aexit__(self, *a):
            return False

    def _no_disk():
        raise OSError("read-only file system")

    monkeypatch.setattr(fg, "AsyncSessionLocal", lambda: _Broken())
    monkeypatch.setattr(fg, "flex_statement_dir", _no_disk)

    data = await IBKRService(token="t", query_id="q").fetch_flex_data()
    assert data["account_id"] == "U0"
    assert served["send_request"] == 1


def test_only_the_newest_statements_are_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(fg, "flex_statement_dir", lambda: tmp_path)
    for n in range(fg.FLEX_STATEMENTS_KEPT + 3):
        fg._keep_statement(b"<x n='%d'/>" % n, NOW + timedelta(days=n))

    kept = sorted(tmp_path.iterdir())
    assert len(kept) == fg.FLEX_STATEMENTS_KEPT
    assert kept[-1].read_bytes() == b"<x n='%d'/>" % (fg.FLEX_STATEMENTS_KEPT + 2)
    assert kept[0].read_bytes() == b"<x n='3'/>"
    assert all(p.suffix == ".xml" for p in kept)  # no `.part` left behind


def test_the_statements_live_beside_the_database(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fg.settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'p.db').as_posix()}"
    )
    assert fg.flex_statement_dir() == (tmp_path / "data" / fg.FLEX_STATEMENT_DIRNAME).resolve()
    monkeypatch.setattr(fg.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    assert fg.flex_statement_dir() is None


# ── the spend row is invisible to everything that asks "was the data refreshed?" ──

def test_the_spend_row_is_in_no_freshness_tuple():
    assert fg.FLEX_GENERATION_SYNC_TYPE not in fg.FLEX_API_SYNC_TYPES
    assert fg.FLEX_GENERATION_SYNC_TYPE not in fg.IBKR_SYNC_TYPES
    assert fg.FLEX_GENERATION_SYNC_TYPE not in ss.SCHEDULED_JOB_TYPES
    assert fg.FLEX_GENERATION_SYNC_TYPE in fg.FLEX_WEB_SERVICE_SYNC_TYPES


class _NoMemory:
    last_sync_result = None


async def _freshness(mk):
    """What every reader of "was the data refreshed?" reports."""
    async with mk() as db:
        svc = ss.SchedulerService()
        return (
            await fg.newest_coverage(db),
            await fg.flex_window_days(db, as_of=NOW),
            await svc.find_stale_ibkr_sync(db, as_of=NOW),
            await svc.find_flex_generation_gap(db, as_of=NOW),
            await _last_sync(_NoMemory(), db),
        )


@pytest.mark.asyncio
async def test_a_served_but_unparsed_statement_refreshes_nothing(maker):
    """
    Ten days without a successful ingest, then a generation served today whose ingest
    failed: the day is spent, and every freshness reader reports exactly what it did before.
    """
    await _add(maker, "ibkr_sync", "success", NOW - timedelta(days=10), details={
        "ibkr_result": {"data_from": "2026-07-01", "data_to": "2026-07-28"},
    })
    before = await _freshness(maker)
    assert before[2] != []  # the stale alarm is firing, so the comparison is not vacuous

    await _add(maker, fg.FLEX_GENERATION_SYNC_TYPE, "success", NOW - timedelta(hours=1))

    assert await _freshness(maker) == before
    async with maker() as db:
        assert await fg.last_generation_today(db, as_of=NOW) == NOW - timedelta(hours=1)


# ── a refusal is remembered ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_press_after_a_lockout_is_refused_until_forced(maker, monkeypatch):
    """The third repro: a press 121 s after a `1025` used to ask the locked token again."""
    calls = _refused(monkeypatch, "1025", "Too many failed attempts.")

    async with maker() as db:
        with pytest.raises(HTTPException) as first:
            await sync_router.sync_ibkr_data(force=False, db=db)
        assert first.value.status_code == 500

        _cool_down()
        with pytest.raises(HTTPException) as second:
            await sync_router.sync_ibkr_data(force=False, db=db)
        assert second.value.status_code == 429
        assert "Code=1025" in second.value.detail
        retry_after = int(second.value.headers["Retry-After"])
        assert 0 < retry_after <= fg.FLEX_LOCKOUT_HOLD_HOURS * 3600 + 1
        assert calls["send_request"] == 1

        # `force` is the owner's override, as for the already-generated skip.
        _cool_down()
        with pytest.raises(HTTPException):
            await sync_router.sync_ibkr_data(force=True, db=db)
    assert calls["send_request"] == 2


@pytest.mark.asyncio
async def test_a_request_step_1001_holds_presses_for_the_rest_of_the_et_day(
    maker, monkeypatch
):
    calls = _refused(monkeypatch, "1001", "Statement could not be generated at this time.")

    async with maker() as db:
        with pytest.raises(HTTPException):
            await sync_router.sync_ibkr_data(force=False, db=db)
        _cool_down()
        with pytest.raises(HTTPException) as second:
            await sync_router.sync_ibkr_data(force=False, db=db)
        assert second.value.status_code == 429
        assert "Code=1001" in second.value.detail
        assert calls["send_request"] == 1

        hold = await fg.flex_refusal_hold(db)
        assert hold.until == fg.next_generation_opens_at(hold.refused_at)
        assert await fg.flex_refusal_hold(db, as_of=hold.until) is None


@pytest.mark.asyncio
async def test_the_scheduled_slots_consult_the_same_hold(maker, monkeypatch):
    message = await _refusal_message(monkeypatch, "1025")
    await _add(
        maker, "full_sync", "error", utcnow() - timedelta(hours=6),
        message=f"Failed to sync IBKR data: {message}",
    )

    class _ExplodingFlex:
        def __init__(self, *a, **k):
            raise AssertionError("a held slot constructed the Flex client")

    monkeypatch.setattr(ss, "IBKRService", _ExplodingFlex)
    result = await _scheduler(monkeypatch)._ibkr_only_sync_locked()

    assert result["status"] == "skipped"
    assert result["reason"] == "flex_refusal_hold"
    assert "Code=1025" in result["ibkr_result"]["message"]


@pytest.mark.asyncio
async def test_after_a_request_step_1001_the_recovery_slot_asks_once_and_a_press_does_not(
    maker, monkeypatch, request
):
    """
    The `1001` half holds manual presses only. After an 18:00 request-step refusal the
    00:00 slot of the same ET day keeps its one fresh SendRequest — "fail fast and let the
    next slot ask fresh", the recovery that saved 08-02 and 08-03.
    """
    message = await _refusal_message(monkeypatch, "1001")
    served = request.getfixturevalue("served")  # from here on, IBKR serves
    await _add(
        maker, "full_sync", "error", utcnow() - timedelta(minutes=1),
        message=f"Failed to sync IBKR data: {message}",
    )

    async with maker() as db:
        with pytest.raises(HTTPException) as press:
            await sync_router.sync_ibkr_data(force=False, db=db)
    assert press.value.status_code == 429
    assert served["send_request"] == 0

    async def _ok(db, flex_data):
        return {"securities_synced": 1, "taxlots_synced": 1, "warnings": []}

    monkeypatch.setattr(ss, "ingest_flex_statement", _ok)
    result = await _scheduler(monkeypatch)._ibkr_only_sync_locked()
    assert result["status"] == "success"
    assert served["send_request"] == 1


@pytest.mark.asyncio
async def test_the_lockout_hold_lifts_after_its_bound(maker, monkeypatch):
    message = await _refusal_message(monkeypatch, "1025")
    await _add(maker, "ibkr", "error", NOW, message=message)
    bound = timedelta(hours=fg.FLEX_LOCKOUT_HOLD_HOURS)

    async with maker() as db:
        assert (await fg.flex_refusal_hold(db, as_of=NOW + bound - timedelta(minutes=1))).code == "1025"
        assert await fg.flex_refusal_hold(db, as_of=NOW + bound) is None


@pytest.mark.asyncio
async def test_a_newer_outcome_lifts_the_hold_and_a_skip_does_not(maker, monkeypatch):
    message = await _refusal_message(monkeypatch, "1025")
    await _add(maker, "ibkr", "error", NOW - timedelta(hours=2), message=message)
    await _add(maker, "ibkr_sync", "skipped", NOW - timedelta(hours=1), message="held")

    async with maker() as db:
        assert await fg.flex_refusal_hold(db, as_of=NOW) is not None

    await _add(maker, fg.FLEX_GENERATION_SYNC_TYPE, "success", NOW - timedelta(minutes=30))
    async with maker() as db:
        assert await fg.flex_refusal_hold(db, as_of=NOW) is None


@pytest.mark.asyncio
async def test_only_a_lockout_or_a_request_step_1001_holds(maker, monkeypatch):
    """Every other refusal message merely *mentions* Code=1025; none of them may hold."""
    not_holding = [
        await _refusal_message(monkeypatch, "1009"),  # request-step refusal, not a 1001
        str(IBKRService._possibly_delivered_error(TimeoutError())),
        "IBKR Flex statement 123 was still not ready after 900s of polling "
        "(last: Code=1001: Statement could not be generated at this time.)",
        "ingest refused",
    ]
    async with maker() as db:
        for i, message in enumerate(not_holding):
            db.add(SyncRun(sync_type="ibkr", status="error", message=message,
                           finished_at=NOW + timedelta(seconds=i)))
            await db.commit()
            assert await fg.flex_refusal_hold(db, as_of=NOW + timedelta(seconds=i)) is None, message


@pytest.mark.asyncio
async def test_the_classifier_reads_the_messages_ibkr_service_actually_raises(monkeypatch):
    lockout = await _refusal_message(monkeypatch, "1025")
    refused = await _refusal_message(monkeypatch, "1001")

    assert fg.flex_refusal_code(lockout) == "1025"
    assert fg.flex_refusal_code(refused) == "1001"
    # As the scheduled jobs record them, behind their own prefix.
    assert fg.flex_refusal_code(f"Failed to sync IBKR data: {lockout}") == "1025"
    # The hold's own message is recorded on a skip, and must never read as a refusal.
    for code in ("1025", "1001"):
        hold = fg.FlexRefusalHold(code, NOW, NOW + timedelta(hours=1))
        assert fg.flex_refusal_code(fg.describe_refusal_hold(hold)) is None


def test_a_lockout_met_at_the_recovery_slot_never_costs_the_next_primary_slot():
    """
    A skip writes no Flex outcome, so only the bound lifts a hold. It must lift before the
    next primary slot after any recovery slot, or one lockout costs a whole day's generation.
    """
    for recovery in ss.IBKR_ONLY_HOURS:
        gap = (ss.FULL_SYNC_HOUR - recovery) % 24 or 24
        assert fg.FLEX_LOCKOUT_HOLD_HOURS < gap
