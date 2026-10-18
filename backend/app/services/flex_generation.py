"""
IBKR's one statement generation per day, and how to tell whether it is spent.

**IBKR generates this account's Flex statement about once per US-Eastern calendar day.**
Every attempt after the day's first success is refused with `Code=1001` at the
*SendRequest* step — the fatal-fast kind, not the "keep polling" kind. Read off this
account's own `sync_runs` on 2026-08-08, grouped by ET day:

    ET day   00:00 ET   02:00 ET   12-13 ET   18:00 ET
    08-01    success    error                 error
    08-02    error      success    error      error
    08-03    error      success    error      error
    08-04    success    error                 error
    08-05    success    error                 error
    08-06    success    error      error      error
    08-07    success    error                 error
    08-08    success    error

Exactly one success per ET day, always the earliest attempt that works, everything
after it refused. Twelve days, zero counterexamples. The only two-success ET day in
the whole history is 07-31, the day the query definition was edited in the portal —
an edit appears to reset it, which is why the callers keep a `force` escape hatch.

**Asking anyway is not free, which is the reason this module exists rather than a
comment.** A `1001` at the request step means IBKR tried to generate and failed, and
failed generations are precisely what `Code=1025` counts — the undocumented token
lockout that has cost this account hours of syncing three times. So a doomed attempt
does not merely produce a red row in the history; it spends the budget that protects
every future sync.

Naive-UTC in, naive-UTC out, per `app/clock.py`: these values are compared against
`sync_runs.finished_at`, which is naive UTC.
"""

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.database import AsyncSessionLocal
from app.db_guard import sqlite_database_path
from app.models.sync_run import SyncRun
from app.repositories.sync_run_repository import SyncRunRepository, utc_iso

logger = logging.getLogger(__name__)

# IBKR's statement day is measured in New York — not Berlin and not UTC. `whenGenerated`
# is stamped in ET, and the "Last N Calendar Days" window rolls at midnight there: a
# statement downloaded at 05:40 Berlin still read `to=20260805`, because that is 23:40
# the previous day in New York. Resolved from the system tz database (verified present
# in `python:3.11-slim`), so no extra dependency.
ET = ZoneInfo("America/New_York")

# Sync types that spend IBKR's one daily generation, i.e. that call the Flex Web Service.
#
# `ibkr_manual_xml` is deliberately absent, and that is empirical rather than assumed.
# The browser download and the Web Service serve the same statement over **independent**
# channels, so an offline ingest costs no generation — proven twice in this account's
# history, where an offline ingest was followed by a *successful* API generation in the
# same ET day (07-28: 00:16 ET then 02:05 ET; 07-31: 00:07 ET then 02:07 ET). Counting
# it here would suppress the day's real attempt on exactly the days someone had just
# recovered by hand.
FLEX_API_SYNC_TYPES = ('ibkr', 'ibkr_sync', 'full_sync')

# Sync types that mean "the data was refreshed", which is a different question from the
# one above and must not be collapsed into it: the offline path genuinely updates the
# portfolio, so it resets a staleness clock even though it spends no generation. Written
# as an extension of the set above rather than as a second literal, because two tuples
# listing overlapping sync types is precisely how this codebase's duplicated logic drifts.
IBKR_SYNC_TYPES = FLEX_API_SYNC_TYPES + ('ibkr_manual_xml',)

# The row `IBKRService.fetch_flex_data` writes the moment IBKR hands back statement bytes,
# in its own committed session, before anything parses or ingests them (GitHub issue #55).
# Until then the spend was recorded only by the run's final `success` row — after the
# ingest, and for `full_sync` after FX, the 730-day pass, dividends and the look-through —
# so a failed ingest or a restart in that tail left the day looking unspent, and the 00:00
# slot and every manual press sent another SendRequest: a `1001`, which `1025` counts.
#
# In neither tuple above, on purpose: it says a generation was *spent*, not that the data
# was refreshed (the ingest may have failed), so the staleness alarms, `newest_coverage`,
# `flex_window_days` and the dashboard's "last sync" must not see it.
FLEX_GENERATION_SYNC_TYPE = 'ibkr_generation'

# Every row that records an exchange with the Flex Web Service: what `last_generation_today`
# counts (its successes) and what `flex_refusal_hold` reads the newest outcome from.
FLEX_WEB_SERVICE_SYNC_TYPES = FLEX_API_SYNC_TYPES + (FLEX_GENERATION_SYNC_TYPE,)

# The raw statement `record_generation_served` keeps, so a failed ingest can be re-run by
# `app/cli/ingest_flex_xml.py` with no token spend. A subdirectory beside the database —
# `/app/data/flex-statements` on the production mount — for the reason `app/yf_cache.py`
# gives: `db_guard`, `db-layout.sh` and the backup look at `portfolio.db` by name only.
# Bounded, because a statement is account data and only the newest few are worth re-running.
FLEX_STATEMENT_DIRNAME = 'flex-statements'
FLEX_STATEMENTS_KEPT = 5


def et_date(moment: datetime) -> date:
    """The US-Eastern calendar date a naive-UTC instant falls on."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(ET).date()


def et_day_bounds_utc(moment: datetime) -> tuple[datetime, datetime]:
    """
    The `[start, end)` of `moment`'s ET calendar day, as naive UTC.

    Bounded at both ends rather than just below so an injected `as_of` cannot see rows
    from after it — the tests pin the boundary from both sides, which is what catches a
    UTC-vs-ET slip. Midnight is safe to construct in any US DST year: the transitions
    happen at 02:00 local, so 00:00 always exists and is never ambiguous.
    """
    day = et_date(moment)
    start = datetime.combine(day, time.min, tzinfo=ET)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=ET)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def next_generation_opens_at(as_of: Optional[datetime] = None) -> datetime:
    """
    When IBKR can next be asked for a statement — the next midnight ET, as naive UTC.

    Note this is also the moment the *window* rolls forward, so it is genuinely "when
    newer data exists", not merely when the refusal lifts.
    """
    _, end = et_day_bounds_utc(as_of or utcnow())
    return end


# ── Which ET days a generation can bring anything new ─────────────────────────
#
# The window ends at the last completed *trading* day before the generation day (a
# Monday 2026-08-24 statement came back `toDate=20260821`, a Friday). So an ET Sunday or
# ET Monday generation reaches the same Friday that Saturday's did, and holds no new
# trading day. They were also where the failures were: 2026-08-08 → 10-14, by ET weekday,
# Sunday and Monday held ~90% of every failed scheduled generation — each one counted by
# `Code=1025` — while Tuesday to Saturday at 12:00 ET went 42 for 43. Whatever a late
# booking adds to a weekend statement, Tuesday's re-delivers: the window is ≥ 30 days.
#
# US market holidays are deliberately not modelled: a generation the day after one is
# still attempted, which is the behaviour this module had before.


def is_trading_day(day: date) -> bool:
    """Monday to Friday. US market holidays are not modelled (see above)."""
    return day.weekday() < 5


def generation_brings_new_trading_day(as_of: Optional[datetime] = None) -> bool:
    """
    Whether a statement generated in `as_of`'s ET day would reach a trading day the
    previous ET day's statement could not — i.e. whether the ET day before it traded.

    False on ET Sunday and ET Monday. The one rule every scheduled IBKR path consults
    before asking; the manual endpoint does not, because pressing Sync is the owner's call.
    Note 00:00 Berlin is 18:00 ET of the *previous* ET day, so the Tuesday 00:00 Berlin
    slot is a Monday ET attempt and is skipped too — this keys on ET, never on Berlin.
    """
    day = et_date(as_of or utcnow())
    return is_trading_day(day - timedelta(days=1))


def next_new_trading_day_generation_opens_at(as_of: Optional[datetime] = None) -> datetime:
    """The next midnight ET from which `generation_brings_new_trading_day` holds, as naive UTC."""
    opens = next_generation_opens_at(as_of)
    while not generation_brings_new_trading_day(opens):
        opens = next_generation_opens_at(opens)
    return opens


def oldest_uncovered_trading_day(generated_on: date) -> date:
    """
    The first trading day a statement generated on ET day `generated_on` does not cover.

    The statement reaches the last trading day *before* `generated_on`, so this is
    `generated_on` itself when that is a trading day, and the following Monday when it
    was a Saturday or Sunday. It is the day whose trades are first at risk if no later
    statement lands — what the gap alarm counts from.
    """
    day = generated_on
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


async def last_generation_today(
    db: AsyncSession, as_of: Optional[datetime] = None
) -> Optional[datetime]:
    """
    When today's Flex generation was spent, or None if it is still available.

    Keys on a **successful** run: a day full of failures has not consumed anything, and
    must not stop the next slot from trying. That is the case the 08-02 and 08-03
    recoveries depended on. "Successful" includes the `FLEX_GENERATION_SYNC_TYPE` row
    written the moment IBKR served bytes, so a statement whose ingest then failed still
    counts: IBKR would refuse the next request of the day with a counted `1001`.
    """
    as_of = as_of or utcnow()
    start, end = et_day_bounds_utc(as_of)

    result = await db.execute(
        select(func.max(SyncRun.finished_at)).where(
            and_(
                SyncRun.sync_type.in_(FLEX_WEB_SERVICE_SYNC_TYPES),
                SyncRun.status == 'success',
                SyncRun.finished_at >= start,
                SyncRun.finished_at < end,
            )
        )
    )
    return result.scalar_one_or_none()


async def generation_already_spent(
    db: AsyncSession, as_of: Optional[datetime] = None
) -> Optional[datetime]:
    """
    `last_generation_today`, but **failing open** — the answer every caller should use.

    An unreadable sync history means we cannot tell whether today's generation is spent,
    and the two ways to be wrong are not symmetric. Failing *closed* would skip the sync,
    and a skipped sync is invisible: no error, no warning, just a portfolio that quietly
    stops updating — the exact silent-outage shape that hid the 730-day pass for four
    days. Failing *open* costs at most one refused request.

    Kept separate from `last_generation_today` rather than swallowing inside it, so the
    query stays honest for tests and so the policy is stated once for both callers
    instead of being decided twice.
    """
    try:
        return await last_generation_today(db, as_of)
    except Exception as e:
        logger.warning(
            "Could not tell whether today's Flex generation is spent (%s) — asking IBKR "
            "anyway rather than skipping a sync", e,
        )
        return None


def flex_statement_dir() -> Optional[Path]:
    """`flex-statements/` beside the SQLite database, absolute; None for a non-file database."""
    db_path = sqlite_database_path(settings.database_url)
    if db_path is None:
        return None
    return (db_path.parent / FLEX_STATEMENT_DIRNAME).resolve()


def _keep_statement(xml: bytes, served_at: datetime) -> Optional[Path]:
    """
    Write `xml` as `flex-<UTC stamp>Z.xml` and delete all but the newest
    `FLEX_STATEMENTS_KEPT`. Written under a `.part` name and renamed, so a crash cannot
    leave a truncated file that looks re-runnable.
    """
    directory = flex_statement_dir()
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"flex-{served_at:%Y%m%dT%H%M%S}Z.xml"
    partial = path.with_name(path.name + '.part')
    partial.write_bytes(xml)
    partial.replace(path)
    for old in sorted(directory.glob('flex-*.xml'))[:-FLEX_STATEMENTS_KEPT]:
        old.unlink(missing_ok=True)
    return path


async def record_generation_served(xml: bytes) -> None:
    """
    Record that IBKR served a generation, and keep the statement — before anything parses it.

    Called by `IBKRService.fetch_flex_data` the moment the download returns bytes, so what
    follows (the parse, the wipe guard, the ingest, a `full_sync`'s long tail, a restart)
    can fail without the day looking unspent. Its own session and commit, never the
    caller's: the caller's transaction is the ingest's, and rolls back with it.

    Best effort, failing open like `generation_already_spent`: a row that cannot be written
    costs at most the refused request this exists to prevent, while raising here would
    throw away a statement IBKR has already served.
    """
    served_at = utcnow()
    kept: Optional[Path] = None
    try:
        kept = await asyncio.get_running_loop().run_in_executor(
            None, _keep_statement, xml, served_at
        )
    except Exception as e:
        logger.warning(f"Could not keep the served Flex statement on disk: {e}")
    try:
        async with AsyncSessionLocal() as db:
            await SyncRunRepository(db).record(
                sync_type=FLEX_GENERATION_SYNC_TYPE,
                status='success',
                message=(
                    f"IBKR served a Flex statement; raw XML kept as {kept.name}" if kept
                    else "IBKR served a Flex statement; raw XML not kept"
                ),
                details={'bytes': len(xml), 'xml_file': kept.name if kept else None},
                started_at=served_at,
            )
    except Exception as e:
        logger.warning(f"Could not record the served Flex generation: {e}")


# ── Remembering a refusal ─────────────────────────────────────────────────────
#
# Until GitHub issue #55 nothing did: after a `1025` the manual sync's only fence was its
# 120 s start cooldown, so a press two minutes later sent SendRequest into the locked
# token — "retrying can extend it". Now the newest Flex outcome on record is read first.
# A `1025` holds the manual endpoint and every scheduled IBKR path alike. A request-step
# `1001` holds manual presses only: the 00:00 recovery slot keeps its one fresh SendRequest
# after an 18:00 refusal ("fail fast and let the next slot ask fresh"), which is how the
# same-ET-day recoveries of 08-02 and 08-03 succeeded.
#
# The phrases IBKRService builds its two refusal messages from. Defined here and imported
# there, so the classifier and the messages cannot drift apart; matching the phrase and
# not a bare `Code=1025` matters, because the other refusal messages *mention* `1025`.
FLEX_LOCKOUT_PHRASE = 'has temporarily locked this Flex token'
FLEX_REQUEST_REFUSED_PHRASE = 'did not hand back a Flex statement reference'
_REFUSAL_MESSAGE = re.compile(
    rf"(?:{re.escape(FLEX_LOCKOUT_PHRASE)}|{re.escape(FLEX_REQUEST_REFUSED_PHRASE)})"
    r" \(Code=(\d+)"
)

# How long a recorded `Code=1025` holds every non-force attempt. Long enough to cover the
# 00:00 recovery slot six hours after an 18:00 lockout and the hours of button presses a
# lockout invites (one lasted ~14 h). **Shorter than 18 h**, the gap from the 00:00 slot to
# the next 18:00 one, so a lockout met at 00:00 never costs the next day's primary slot:
# a skip writes no Flex outcome, so nothing but this bound lifts the hold.
FLEX_LOCKOUT_HOLD_HOURS = 12


class FlexRefusalHold(NamedTuple):
    code: str              # '1025', or '1001' refused at the request step
    refused_at: datetime   # naive UTC, the refusal's `finished_at`
    until: datetime        # naive UTC, when asking is allowed again


def flex_refusal_code(message: Optional[str]) -> Optional[str]:
    """The code of a lockout or request-step refusal recorded in `message`, else None."""
    match = _REFUSAL_MESSAGE.search(message or '')
    return match.group(1) if match else None


async def flex_refusal_hold(
    db: AsyncSession, as_of: Optional[datetime] = None, request_refusals: bool = True
) -> Optional[FlexRefusalHold]:
    """
    Whether the newest Flex Web Service outcome on record says not to ask IBKR yet.

    Reads the newest success or error among `FLEX_WEB_SERVICE_SYNC_TYPES` — a skip asked
    nothing, so it neither holds nor lifts. It holds when that row is a `Code=1025` younger
    than `FLEX_LOCKOUT_HOLD_HOURS`, or — only with `request_refusals`, i.e. for a manual
    press — a `1001` at the SendRequest step in the same ET day (IBKR refused that day's
    generation; pressing again is another counted failure, and the next ET day is a new
    generation). The scheduled slots pass `request_refusals=False`: the recovery slot's
    one fresh request after a refusal is the schedule's design. Any later success, or any
    other error, lifts it.
    """
    as_of = as_of or utcnow()
    row = (await db.execute(
        select(SyncRun.status, SyncRun.message, SyncRun.finished_at)
        .where(
            and_(
                SyncRun.sync_type.in_(FLEX_WEB_SERVICE_SYNC_TYPES),
                SyncRun.status.in_(('success', 'error')),
                SyncRun.finished_at <= as_of,
            )
        )
        .order_by(SyncRun.finished_at.desc(), SyncRun.id.desc())
        .limit(1)
    )).first()
    if row is None or row.status != 'error':
        return None
    code = flex_refusal_code(row.message)
    if code == '1025':
        until = row.finished_at + timedelta(hours=FLEX_LOCKOUT_HOLD_HOURS)
    elif code == '1001' and request_refusals:
        until = next_generation_opens_at(row.finished_at)
    else:
        return None
    return FlexRefusalHold(code, row.finished_at, until) if as_of < until else None


async def flex_refusal_in_force(
    db: AsyncSession, as_of: Optional[datetime] = None, request_refusals: bool = True
) -> Optional[FlexRefusalHold]:
    """`flex_refusal_hold`, failing open for `generation_already_spent`'s reason."""
    try:
        return await flex_refusal_hold(db, as_of, request_refusals)
    except Exception as e:
        logger.warning(
            "Could not read the last Flex refusal (%s) — asking IBKR anyway rather than "
            "skipping a sync", e,
        )
        return None


def describe_refusal_hold(hold: FlexRefusalHold) -> str:
    """The one message both callers give for a hold. Must not match `_REFUSAL_MESSAGE`."""
    if hold.code == '1025':
        what = (
            f"IBKR locked the Flex token (Code=1025) at {utc_iso(hold.refused_at)}, and "
            "asking during a lockout can extend it."
        )
    else:
        what = (
            f"IBKR refused today's statement at the request step (Code={hold.code}) at "
            f"{utc_iso(hold.refused_at)}, and another refusal counts toward the Code=1025 "
            "lockout."
        )
    return f"{what} Not asking IBKR again before {utc_iso(hold.until)}; force=true overrides."


# Fallback window length, in calendar days, when no successful statement is on record
# to measure — a fresh install, or a `details` blob that carries no span.
#
# Three, and deliberately the narrowest period this account has ever run, because the
# only consumer is an alarm threshold and the two ways to be wrong are not symmetric:
# guessing narrow warns early (cost: one warning line against a window that had slack),
# guessing wide warns late (cost: trades no future statement contains). Same asymmetry
# `generation_already_spent` resolves by failing open.
FLEX_WINDOW_DAYS_FALLBACK = 3

# How many recent successful runs `flex_window_days` will look through for a recorded
# span. More than one because a success is not guaranteed to carry the keys — a run
# recorded before the span was persisted, or one whose `details` were truncated, would
# otherwise send the measurement to the fallback while a perfectly good statement sat one
# row below. Small enough that a genuine period change is still picked up within a day.
_WINDOW_MEASUREMENT_RUNS = 5


def _statement_span(details: Optional[dict]) -> Optional[tuple[date, date]]:
    """
    The `data_from`/`data_to` a recorded run reports, from either shape it is written in.

    Two shapes exist because two callers record it: the scheduled jobs nest their IBKR
    step under `details['ibkr_result']`, while the manual endpoint and the offline CLI
    put the same keys at the top level. Reading only one of them is how this measurement
    would go quietly blind on half the history.
    """
    if not isinstance(details, dict):
        return None
    for scope in (details, details.get('ibkr_result')):
        if not isinstance(scope, dict):
            continue
        raw_from, raw_to = scope.get('data_from'), scope.get('data_to')
        if not raw_from or not raw_to:
            continue
        try:
            return date.fromisoformat(str(raw_from)), date.fromisoformat(str(raw_to))
        except ValueError:
            continue
    return None


async def newest_coverage(db: AsyncSession) -> Optional[tuple[datetime, date]]:
    """
    The successful IBKR run whose statement reaches furthest forward, as
    `(its finished_at, the oldest trading day it does not cover)`. None if none succeeded.

    **Keyed on what a run covered, not on when it finished.** An `ibkr_manual_xml` ingest
    of a prior-range download finishes today and covers nothing recent; counting from its
    `finished_at` told both staleness alarms the data was fresh while every trade since
    the last API statement was still at risk (GitHub issue #13).

    A run's reach is the trading day after its recorded `data_to`, capped by the one
    its finish date implies: no statement covers a day after it was generated, and a
    custom range ending today does not cover trades booked after the download. A run
    with no recorded span falls back to the cap alone — the finished_at count this
    replaced. Walked newest first and stopped once no older run's cap can beat the best
    reach so far, which is usually the second row.
    """
    result = await db.stream(
        select(SyncRun.finished_at, SyncRun.details)
        .where(
            and_(
                SyncRun.sync_type.in_(IBKR_SYNC_TYPES),
                SyncRun.status == 'success',
                SyncRun.finished_at.isnot(None),
            )
        )
        .order_by(SyncRun.finished_at.desc())
    )
    best: Optional[tuple[datetime, date]] = None
    try:
        async for finished_at, details in result:
            cap = oldest_uncovered_trading_day(et_date(finished_at))
            if best is not None and cap <= best[1]:
                break
            span = _statement_span(details)
            reach = cap if span is None else min(
                cap, oldest_uncovered_trading_day(span[1] + timedelta(days=1))
            )
            if best is None or reach > best[1]:
                best = (finished_at, reach)
    finally:
        await result.close()
    return best


async def flex_window_days(
    db: AsyncSession, as_of: Optional[datetime] = None
) -> Optional[int]:
    """
    How many calendar days the Flex Query actually reaches back, measured not declared.

    **The period is a portal setting, so no constant in this repo can be trusted to
    match it.** It has been `Year to Date`, then `Last 30 Calendar Days`, then
    `Last 3 Calendar Days`, and on 2026-08-24 the live query was found to be back at 30
    while `FLEX_GENERATION_GAP_WARN_DAYS` was still derived from 3 — so the gap alarm
    fired 26 days before the margin it was protecting had actually run out, with a
    message asserting trades were "about to become unreachable" when ~28 days of slack
    remained. A threshold hand-derived from a number a human maintains in a comment is
    a threshold that is wrong whenever somebody edits the portal and not this file.

    So it is read off the statement IBKR served: `data_to - data_from`, inclusive, from
    the most recent successful run. That span is already recorded on every run and needs
    no new column, no new request, and nobody to remember anything.

    Measured over `FLEX_API_SYNC_TYPES` only, which is the same distinction the two type
    sets above exist for. An `ibkr_manual_xml` ingest is a browser download whose period
    is whatever the operator typed — that is the documented *recovery* for a gap, often
    deliberately wider than the query — so letting it define the window would take one
    hand-widened statement and quietly relax the alarm for every day after it.

    `None` when nothing is on record, leaving the threshold to
    `FLEX_WINDOW_DAYS_FALLBACK` rather than to a guess made here.
    """
    result = await db.execute(
        select(SyncRun.details)
        .where(
            and_(
                SyncRun.sync_type.in_(FLEX_API_SYNC_TYPES),
                SyncRun.status == 'success',
            )
        )
        .order_by(SyncRun.finished_at.desc())
        .limit(_WINDOW_MEASUREMENT_RUNS)
    )
    for (details,) in result.all():
        span = _statement_span(details)
        if span is None:
            continue
        start, end = span
        days = (end - start).days + 1
        if days >= 1:
            return days
    return None
