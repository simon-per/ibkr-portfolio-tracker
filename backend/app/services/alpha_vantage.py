"""
Alpha Vantage: the one module that asks it, under one daily budget for every caller
(docs/flex-and-sync.md *Alpha Vantage*, docs/company-financials.md *Alpha Vantage, last*).

Two callers spend one key: the price fallback (`MarketDataService.
fetch_prices_from_alpha_vantage`, a US listing Yahoo returned nothing for) and the
Company tab's statement fill (`company_vendor_fill`, the gaps SEC left). Each used to be
able to spend the whole free tier on its own — the price fallback latched its cap per
pass only, so a restart or the next pass asked again — and two copies of "how to ask
Alpha Vantage" would drift the way this codebase's copies do (CLAUDE.md, *The dominant
failure mode*). So the URL, the key, the pacing, the budget and the reading of a refusal
live here, and `tests/test_alpha_vantage_family.py` fails any other module that names
`alphavantage.co`.

- **The budget is a UTC day's** (`DAILY_LIMIT`, the free tier's 25 requests), persisted in
  `app_settings` (`SPENT_KEY`: `"<date> <n>"`), so a restart or a deploy does not reset it.
  A call is counted before it is made — the free tier counts it whatever it answers.
- **The price fallback goes first** (owner, 2026-11-16): the statement fill may spend only
  what leaves `PRICE_RESERVE` calls for it, every day; prices may spend all of it.
- **A refusal holds the rest of the UTC day.** Alpha Vantage answers its limit with HTTP 200
  and a JSON `Note` (older) or `Information` notice (current; also a premium-only parameter),
  never with a status code. Either is read as "capped" and persisted (`CAPPED_KEY`), so no
  caller asks again until the next UTC day — never retried within it. That is how the key's
  real tier shows: a stricter limit than `DAILY_LIMIT` surfaces as an early notice.
- **Paced** at 5 requests a minute (`MIN_INTERVAL_S`), the free tier's burst limit.
- **The key never reaches a log line.** It travels as the `apikey` query parameter, so a
  transport error's text (which carries the URL) is never logged — only its type — and
  `app/redact.py` masks `apikey=` and the key's value wherever a message is stored anyway.

An `Error Message` (an unknown symbol, a bad function) is an answer: `None`, spent.
"""

import asyncio
import logging
import time
from datetime import date
from typing import Any, Callable, Dict, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.repositories.app_settings_repository import AppSettingsRepository

logger = logging.getLogger(__name__)

URL = "https://www.alphavantage.co/query"
#: The free tier's requests a UTC day.
DAILY_LIMIT = 25
#: Calls the statement fill leaves each UTC day for the price fallback, which goes first.
PRICE_RESERVE = 10
#: The free tier's 5 requests a minute, with a little room.
MIN_INTERVAL_S = 12.5
REQUEST_TIMEOUT_S = 30.0

PRICES = "prices"
STATEMENTS = "statements"
#: What each caller may spend of a UTC day's budget.
LIMITS = {PRICES: DAILY_LIMIT, STATEMENTS: DAILY_LIMIT - PRICE_RESERVE}

SPENT_KEY = "alpha_vantage_spent"
CAPPED_KEY = "alpha_vantage_capped_on"
NOTICES = ("Note", "Information")

_last_call: Optional[float] = None


class AlphaVantageCapped(Exception):
    """No call today: Alpha Vantage said its limit is reached (persisted for the UTC day),
    or this caller's share of the day's budget is spent."""


def is_configured() -> bool:
    """A key is set (read at call time so a monkeypatch counts)."""
    key = (settings.alpha_vantage_api_key or "").strip()
    return bool(key) and key != "your_api_key_here"


def _spent_on(raw: Optional[str], today: date) -> int:
    """The count stored for `today`; another day's (or an unreadable one) is 0."""
    day, _, count = (raw or "").partition(" ")
    if day != today.isoformat():
        return 0
    try:
        return max(int(count), 0)
    except ValueError:
        return 0


async def budget(db: AsyncSession, purpose: str) -> Dict[str, Any]:
    """Today's state for `purpose`: `{spent, left, capped}` — what the runs record."""
    repo = AppSettingsRepository(db)
    today = utcnow().date()
    spent = _spent_on(await repo.get(SPENT_KEY), today)
    capped = (await repo.get(CAPPED_KEY)) == today.isoformat()
    left = 0 if capped else max(LIMITS[purpose] - spent, 0)
    return {"spent": spent, "left": left, "capped": capped}


async def ask(
    db: AsyncSession,
    function: str,
    purpose: str,
    sleep: Optional[Callable[[float], Any]] = None,
    clock: Callable[[], float] = time.monotonic,
    **params: str,
) -> Optional[Dict[str, Any]]:
    """
    One Alpha Vantage call, counted against today's budget for `purpose` (`LIMITS`) and
    committed on `db` before it is made. Returns the JSON body; None when no key is set,
    on an `Error Message` or on a transport failure (both spent). Raises
    `AlphaVantageCapped` when today's budget for `purpose` is spent or Alpha Vantage
    answers with a notice — persisted, so nothing asks again before the next UTC day.
    """
    global _last_call
    if not is_configured():
        return None
    state = await budget(db, purpose)
    if state["capped"]:
        raise AlphaVantageCapped("Alpha Vantage said its limit was reached today")
    if state["left"] <= 0:
        raise AlphaVantageCapped(f"today's Alpha Vantage budget for {purpose} is spent")
    today = utcnow().date()
    await AppSettingsRepository(db).set(SPENT_KEY, f"{today.isoformat()} {state['spent'] + 1}")
    await db.commit()

    if _last_call is not None:
        wait = MIN_INTERVAL_S - (clock() - _last_call)
        if wait > 0:
            await (sleep or asyncio.sleep)(wait)
    _last_call = clock()
    symbol = params.get("symbol", "")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            query = {"function": function, **params}
            query["apikey"] = settings.alpha_vantage_api_key
            response = await client.get(URL, params=query)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        # Never the message: it can carry the URL, and the URL carries the key.
        logger.warning(f"Alpha Vantage {function} for {symbol} failed: {type(e).__name__}")
        return None
    if not isinstance(data, dict):
        return None
    for notice in NOTICES:
        if notice in data:
            await AppSettingsRepository(db).set(CAPPED_KEY, today.isoformat())
            await db.commit()
            logger.warning(
                f"Alpha Vantage {notice} for {symbol}: {data[notice]} — no Alpha Vantage "
                f"request before the next UTC day"
            )
            raise AlphaVantageCapped(f"Alpha Vantage {notice}: {data[notice]}")
    if "Error Message" in data:
        logger.info(f"Alpha Vantage {function} for {symbol}: {data['Error Message']}")
        return None
    return data
