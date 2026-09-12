"""
Keep the look-through's baskets and identities current without a human running the CLIs.

Until 2026-09-08 the constituent baskets were populated by a deliberate CLI run and then
decayed in place: nine of the ten issuer feeds republish daily, `find_stale_etf_baskets`
warned once a basket outgrew its issuer's cadence, and on production that warning had
been present on every market-data run for two weeks — seven funds, the same line seven
times, which is the always-present-banner pathology CLAUDE.md keeps rediscovering. The
data was always reachable by a keyless HTTP call and the `etf_baskets` table was always
the cache; what was missing was the trigger.

The trigger is the 18:00 `full_sync` job, not the read endpoint. `/api/portfolio/lookthrough`
stays pure DB: a public GET that reaches seven third-party sites on a cache miss is a
denial-of-service vector aimed at somebody else's servers, and entering `SYNC_PIPELINE`
from a read would bump the shared last-start clock every other route's cooldown reads.
Once a day, from the job that already holds the gate, is the right cadence for issuers
that publish once a day.

Three rules carry it:

- **The refresh consumes the detector's verdict, never a second definition of "stale".**
  `stale_basket_verdicts` is the one predicate; `find_stale_etf_baskets` formats it into
  warnings and this module fetches from it. Two implementations of "which basket is
  stale" would be CLAUDE.md's dominant failure mode, and the detector had already been
  wrong once about which basket a proxied fund reads.
- **Every refusal the CLI makes, this makes.** `replace_basket` still refuses a row-count
  collapse and a backwards as-of, a parse failure still stores nothing, and the previous
  basket stays in use either way. A scheduled path that could half-apply what the CLI
  refuses would be worse than no scheduled path.
- **One fund's failure is that fund's warning, not the job's error.** An issuer that
  changes a URL takes its own fund stale and nothing else; the IBKR and Yahoo halves of
  the job never see it.

Identity follows the baskets, in two bounded pieces. A re-import clears the CINS/SEDOL
resolutions on purpose (see `EtfBasketRepository.replace_basket`), so the OpenFIGI pass
that restores them runs whenever a basket was replaced — that is the second half of every
GRID or QTUM import, and forgetting it is what the old warning text spent a clause on. And a
bounded ISIN pass (`SCHEDULED_IDENTITY_LIMIT` per evening) walks the held securities and
the material constituents that have never been asked about: GLEIF has no batch form and
costs ~1.1 s per ISIN, so the bound keeps the step to under a minute and the set converges
over a few evenings rather than in one forty-minute run. A definitive answer is cached
either way.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.etf_mappings import is_known_etf_isin, symbol_for_fund_isin
from app.etf_sources import FundSource, basket_proxy_for, source_for_fund_isin
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.etf_basket_repository import (
    BasketReplaceRefused,
    EtfBasketRepository,
)
from app.services.etf_basket_fetch import (
    AUTOMATED_ADAPTERS,
    BETWEEN_REQUESTS_S,
    fetch_bodies,
    new_client,
    parse_bodies,
)
from app.services.identity_service import IdentityService, held_isins
from app.services.lookthrough_service import LookthroughService, stale_after_days

logger = logging.getLogger(__name__)

# ISINs the evening identity pass may ask each provider about. GLEIF is the bound: one
# request per ISIN at ~1.1 s. Twenty-five is under half a minute and converges a rebalance's
# worth of new constituents in a few evenings.
SCHEDULED_IDENTITY_LIMIT = 25


@dataclass(frozen=True)
class StaleBasket:
    """
    One held fund whose basket is missing or older than its issuer's cadence.

    `source_isin` is the basket the fund actually *reads* — itself, or the fund it borrows
    from (`FundSource.basket_proxy_isin`) — and `adapter` is the declared route for that
    source, which is what a refresh would fetch with. `basket_adapter` is the adapter the
    stored basket came from, which is what the staleness threshold was judged against;
    the two differ for a basket somebody imported by hand under `--adapter manual`.
    """
    held_isin: str
    name: str
    source_isin: str
    adapter: Optional[str]
    basket_adapter: Optional[str]
    as_of_date: Optional[date]
    age_days: Optional[int]
    limit_days: Optional[int]

    @property
    def missing(self) -> bool:
        return self.as_of_date is None

    @property
    def fetchable(self) -> bool:
        return self.adapter in AUTOMATED_ADAPTERS


async def _held_fund_isins(db: AsyncSession) -> List[str]:
    held = await db.execute(
        select(distinct(Security.isin))
        .join(TaxLot, TaxLot.security_id == Security.id)
        .where(TaxLot.is_open == True)  # noqa: E712 — SQLAlchemy needs the operator
    )
    return sorted({
        isin.strip().upper() for (isin,) in held.all()
        if isin and is_known_etf_isin(isin)
    })


def _route_for_missing(isin: str, source: Optional[FundSource]):
    """
    Which basket a fund with none of its own would read once fetched, and how to fetch it.

    Its own, when it has an automated route and is not synthetic — a synthetic fund's own
    published basket is substitute collateral that `_alias_proxied_baskets` never lets win,
    so fetching it would store a file nothing reads. Otherwise its proxy's. `(None, None)`
    means nothing the scheduler can run would clear the gap: a hand download, or no
    published file at all.
    """
    own_route = source.adapter if source else None
    own_speaks = source is None or source.replication != "synthetic"
    if own_route in AUTOMATED_ADAPTERS and own_speaks:
        return isin, own_route
    proxy = basket_proxy_for(isin)
    proxy_source = source_for_fund_isin(proxy) if proxy else None
    if proxy_source is not None and proxy_source.adapter in AUTOMATED_ADAPTERS:
        return proxy, proxy_source.adapter
    return None, None


async def stale_basket_verdicts(
    db: AsyncSession, as_of: Optional[date] = None
) -> List[StaleBasket]:
    """
    Held funds whose constituent basket is missing or older than its issuer's cadence.

    The one predicate behind both `SchedulerService.find_stale_etf_baskets` (which formats
    these into `warnings[]`) and `refresh_stale_baskets` (which fetches from them). Four
    rules, each of which would be wrong the other way:

    - **Held funds only.** A basket for a fund nobody holds moves no figure.
    - **The basket a fund actually reads comes from
      `LookthroughService._alias_proxied_baskets`, never from a second copy of the proxy
      rule.** The age that matters is the age of the file the numbers came from, which for
      a proxied fund is the *source's*.
    - **The threshold is `lookthrough_service.stale_after_days`**, the existing per-adapter
      table. A single global constant meant two different things once and badged Vanguard
      permanently for publishing month-end as documented.
    - **Nothing unclearable is reported.** A fund excluded from look-through by design, and
      one with no basket and no route to one, can never be fixed by running anything — and a
      warning that can never clear is the pathology this whole module exists to end. A held
      fund with no basket that *does* have a route is reported, because that is what a newly
      bought fund looks like and one evening clears it.
    """
    as_of = as_of or date.today()
    fund_isins = await _held_fund_isins(db)
    if not fund_isins:
        return []

    # Load each held fund's own basket plus any it borrows, then let the read path's own
    # resolver decide which one each fund actually uses.
    proxy_sources = sorted({
        src for i in fund_isins if (src := basket_proxy_for(i)) and src != i
    })
    baskets = await EtfBasketRepository(db).get_baskets(fund_isins + proxy_sources)
    LookthroughService._alias_proxied_baskets(fund_isins, baskets, {})

    verdicts: List[StaleBasket] = []
    for isin in fund_isins:
        source = source_for_fund_isin(isin)
        if source is not None and not source.look_through_eligible:
            continue  # excluded by design: no run of anything would clear it
        name = symbol_for_fund_isin(isin) or isin
        basket = baskets.get(isin)

        if basket is None:
            source_isin, route = _route_for_missing(isin, source)
            if source_isin is None:
                continue
            verdicts.append(StaleBasket(
                held_isin=isin, name=name, source_isin=source_isin, adapter=route,
                basket_adapter=None, as_of_date=None, age_days=None, limit_days=None,
            ))
            continue

        limit = stale_after_days(basket.adapter)
        age = (as_of - basket.as_of_date).days
        if age <= limit:
            continue
        source_isin = (basket.fund_isin or isin).strip().upper()
        declared = source_for_fund_isin(source_isin)
        verdicts.append(StaleBasket(
            held_isin=isin, name=name, source_isin=source_isin,
            adapter=declared.adapter if declared else None,
            basket_adapter=basket.adapter, as_of_date=basket.as_of_date,
            age_days=age, limit_days=limit,
        ))
    return verdicts


FetchFn = Callable[[object, str, FundSource], Awaitable[Sequence[bytes]]]


async def refresh_stale_baskets(
    db: AsyncSession,
    as_of: Optional[date] = None,
    *,
    client_factory: Optional[Callable[[], object]] = None,
    fetch: Optional[FetchFn] = None,
    parse=None,
) -> Dict:
    """
    Fetch, parse and store a fresh basket for every stale verdict that has a route.

    One fetch per *source* basket: VWCE and VT stale together is one Vanguard walk, and the
    result names every held fund it served. The previous basket survives every failure
    mode — a fetch error, a parse refusal, or `replace_basket` declining a collapse — and
    each is reported as a warning against the fund rather than failing the pass. The
    `fetch`/`parse`/`client_factory` seams exist for tests; production callers pass nothing.
    """
    # Resolved at call time rather than as default values, so a test can patch the module
    # attributes and the production path picks up exactly what the module exports.
    client_factory = client_factory or new_client
    fetch = fetch or fetch_bodies
    parse = parse or parse_bodies

    as_of = as_of or date.today()
    verdicts = await stale_basket_verdicts(db, as_of)
    result: Dict = {
        "status": "success",
        "stale": sorted(v.name for v in verdicts),
        "refreshed": [],
        "refused": [],
        "failed": [],
        "needs_hand_download": sorted(v.name for v in verdicts if not v.fetchable),
        "warnings": [],
    }

    # Group by the basket to fetch, keeping the names of the held funds that read it.
    targets: Dict[str, List[str]] = {}
    for v in verdicts:
        if v.fetchable:
            targets.setdefault(v.source_isin, []).append(v.name)
    if not targets:
        return result

    repo = EtfBasketRepository(db)
    ordered = sorted(targets.items(), key=lambda kv: symbol_for_fund_isin(kv[0]) or kv[0])
    async with client_factory() as client:
        for index, (source_isin, served) in enumerate(ordered):
            if index:
                await asyncio.sleep(BETWEEN_REQUESTS_S)
            source = source_for_fund_isin(source_isin)
            symbol = symbol_for_fund_isin(source_isin) or source_isin
            try:
                bodies = await fetch(client, source_isin, source)
                basket = parse(source_isin, bodies, source.adapter, symbol, as_of)
                stored = await repo.replace_basket(basket)
                await db.commit()
            except BasketReplaceRefused as e:
                await db.rollback()
                result["refused"].append({"symbol": symbol, "for": sorted(served), "reason": str(e)})
                result["warnings"].append(
                    f"{symbol}: the freshly fetched basket was refused and the previous one is "
                    f"still in use — {e}"
                )
                continue
            except Exception as e:
                await db.rollback()
                result["failed"].append({
                    "symbol": symbol, "for": sorted(served),
                    "error": f"{type(e).__name__}: {e}",
                })
                result["warnings"].append(
                    f"{symbol}: basket refresh failed ({type(e).__name__}: {e}); the previous "
                    f"basket is still in use. If this persists, run "
                    f"`python -m app.cli.fetch_etf_baskets {symbol} --out /tmp/baskets` by hand "
                    f"— the saved body is what makes the failure debuggable"
                )
                continue

            result["refreshed"].append({
                "fund_isin": source_isin,
                "symbol": symbol,
                "for": sorted(served),
                "as_of_date": basket.as_of_date.isoformat(),
                "as_of_is_issuer_stated": basket.as_of_is_issuer_stated,
                "stored_rows": stored.stored_rows,
                "previous_rows": stored.previous_rows,
                "previous_as_of": (
                    stored.previous_as_of.isoformat() if stored.previous_as_of else None
                ),
            })
            logger.info(
                f"Basket refreshed: {symbol} as of {basket.as_of_date} "
                f"({stored.stored_rows} rows, was {stored.previous_rows} from {stored.previous_as_of})"
            )
    return result


async def resolve_pending_identities(
    db: AsyncSession, *, limit: int = SCHEDULED_IDENTITY_LIMIT, after_refresh: bool
) -> Dict:
    """
    The two identity passes the look-through needs, bounded for a scheduled run.

    The CINS/SEDOL pass (`resolve_constituent_identifiers`) runs only after a basket was
    replaced, because that is the only event that creates work for it — a re-import clears
    those resolutions on purpose — and it is a handful of OpenFIGI batches. The ISIN pass
    runs every evening but asks each provider about at most `limit` ISINs, so a newly bought
    security folds with its fund exposure within a day and a rebalance's worth of new
    constituents converges over a few.
    """
    service = IdentityService(db)
    targets = await held_isins(db, include_funds=False)
    constituents = await LookthroughService(db).material_constituent_isins()
    known = set(targets)
    targets = targets + [i for i in constituents if i not in known]

    summary = await service.resolve(targets, limit=limit)
    if after_refresh:
        # Bounded like the ISIN pass. This method does not cache a miss, which its
        # docstring accepts only for a hand-run CLI — scheduled and unbounded, the
        # permanently unresolvable identifiers were re-asked of OpenFIGI every evening
        # a basket was replaced, growing with each new fund.
        summary.update(await service.resolve_constituent_identifiers(limit=limit))
    await db.commit()
    return summary


async def refresh_lookthrough_data(db: AsyncSession, as_of: Optional[date] = None) -> Dict:
    """
    Baskets, then identities — the whole evening upkeep, each half guarded separately.

    A failing identity provider must not undo a basket refresh that already committed, and
    a failing issuer must not stop the identity pass from folding what it can.
    """
    result: Dict = {"status": "success", "warnings": []}
    try:
        baskets = await refresh_stale_baskets(db, as_of)
        result["baskets"] = baskets
        result["warnings"].extend(baskets.get("warnings") or [])
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.error(f"Basket refresh failed: {e}")
        result["baskets"] = {"status": "error", "message": str(e)}
        result["warnings"].append(
            f"The look-through basket refresh failed ({type(e).__name__}: {e}); every basket "
            f"is as it was and the stale ones stay stale"
        )
        baskets = {}

    try:
        result["identities"] = await resolve_pending_identities(
            db, after_refresh=bool(baskets.get("refreshed"))
        )
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.error(f"Identity resolution failed: {e}")
        result["identities"] = {"status": "error", "message": str(e)}
        result["warnings"].append(
            f"Identity resolution failed ({type(e).__name__}: {e}); companies whose ISINs "
            f"were never asked about stay as separate look-through rows until it succeeds"
        )
    return result
