"""
Fetch one fund's holdings file from its issuer, in memory, and parse it.

The network half of the basket pipeline, shared by two callers that must not drift:

- `app/cli/fetch_etf_baskets.py` writes the bodies to disk and stops, so a scraper's
  failure mode becomes a committable fixture — the same relationship `ingest_flex_xml.py`
  has to a statement downloaded from Client Portal, and the only way to test the trap this
  feature is most exposed to (the retired iShares URL answers HTTP 200 with
  `Content-Type: text/csv` and an HTML body).
- `app/services/etf_basket_refresh.py` parses the bodies straight away on the 18:00 job,
  so the look-through stops depending on somebody remembering a CLI run.

One fetcher per adapter, one parser dispatch, and both callers use them: a fetcher that
existed twice would be CLAUDE.md's dominant failure mode applied to a scraper, where the
two copies stop agreeing the day an issuer changes a URL.

Touches **no** Yahoo and **no** IBKR, so neither rule at the top of CLAUDE.md applies. It
does reach seven issuer sites, as a guest rather than a customer: a descriptive User-Agent
carrying `LOOKTHROUGH_CONTACT_EMAIL` when set, one request at a time, a pause between them,
and no retry storm. These files are the funds' own regulatory portfolio disclosures — fine
to cache privately for one portfolio, and not ours to redistribute.
"""
import asyncio
import logging
from datetime import date
from typing import List, Sequence

import httpx

from app.etf_sources import FundSource, user_agent
from app.services.etf_basket_parsers import (
    PARSERS,
    BasketParseError,
    ParsedBasket,
    parse_defiance,
    parse_dws,
    parse_first_trust,
    parse_invesco,
    parse_ishares,
    parse_vaneck,
    parse_vanguard_us,
)
from app.services.security_identifiers import cusip_from_isin

logger = logging.getLogger(__name__)

# One at a time, with a pause. Nothing here is rate-limited in any documented way; this is
# simply not being a nuisance to somebody else's web server.
BETWEEN_REQUESTS_S = 2.0
REQUEST_TIMEOUT_S = 60.0

DWS_URL = "https://etf.dws.com/etfdata/export/GBR/ENG/csv/product/constituent/{isin}/"

BLACKROCK_URL = (
    "https://www.blackrock.com/varnish-api/uk-retail01-product-data/product-data/api/v2/"
    "get-product-data"
)
BLACKROCK_PARAMS = {
    "appType": "PRODUCT_PAGE",
    "appSubType": "ISHARES",
    "targetSite": "ishares-uk",
    "locale": "en_GB",
    "userType": "individual",
    "component": "holdings",
}

VANGUARD_URL = (
    "https://investor.vanguard.com/investment-products/etfs/profile/api/{ticker}/"
    "portfolio-holding/stock"
)
# The API caps a page at 500 however large a `count` is asked for, so VT's ~10,000 holdings
# take ~21 requests. The ceiling below is a runaway guard, not a limit on any real fund.
VANGUARD_PAGE = 500
VANGUARD_MAX_PAGES = 60

# Keyed by the fund's own CUSIP, which is the ISIN with its country prefix and check digit
# removed — so nothing has to be discovered or kept in step, unlike BlackRock's portfolio id.
INVESCO_URL = (
    "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/{cusip}/holdings/fund"
)
INVESCO_PARAMS = {"idType": "cusip", "productType": "ETF"}

FIRST_TRUST_URL = "https://www.ftportfolios.com/retail/etf/etfholdings.aspx"

# `-full-holdings`, NOT the plain product page: that one renders its table client-side, so a
# fetch of it returns a document with no holdings in it at all and the parser rightly refuses.
DEFIANCE_URL = "https://www.defianceetfs.com/{slug}-full-holdings/"

# The locale is pinned rather than left to geo-resolution, so the same file comes back from
# any machine. Two GETs, not one: the first is only there to be handed the consent cookies
# that the second needs, without which this URL loops its redirects indefinitely.
VANECK_URL = "https://www.vaneck.com/nl/en/investments/{slug}/downloads/holdings/"
VANECK_CONSENT_URL = "https://www.vaneck.com/nl/en/investments/{slug}/"


# Every adapter with a fetcher below. Derived from the fetchers rather than from
# `etf_sources.ADAPTERS`, which also names `manual` — a real answer, not a route. The stale
# detector and the scheduled refresh both import this set rather than re-listing it, so it
# cannot drift from what actually exists.
AUTOMATED_ADAPTERS = frozenset({
    "dws", "blackrock", "vanguard_us", "invesco", "first_trust", "defiance", "vaneck",
})


class FetchError(Exception):
    """The download failed — reported, and nothing stored for that fund."""


def new_client() -> httpx.AsyncClient:
    """The one client configuration both callers use: etiquette headers, redirects, timeout."""
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(), "Accept": "*/*"},
        timeout=REQUEST_TIMEOUT_S,
        follow_redirects=True,
    )


async def fetch_dws(client: httpx.AsyncClient, isin: str) -> bytes:
    response = await client.get(DWS_URL.format(isin=isin))
    response.raise_for_status()
    return response.content


async def fetch_blackrock(client: httpx.AsyncClient, isin: str, source: FundSource) -> bytes:
    portfolio_id = source.params.get("portfolio_id")
    if not portfolio_id:
        raise FetchError(f"{source.symbol}: no portfolio_id declared in app/etf_sources.py")
    # One host serves every iShares domicile, but the **locale** selects which catalogue it
    # looks the portfolio up in, and a fund absent from that catalogue is a flat
    # `400 BAD_REQUEST_INVALID_PARAM_VALUES` rather than an empty basket. Measured against
    # IQQ (a US-listed fund) on 2026-08-24: `en_GB` 400s, `en_US` returns all 106 rows from
    # the same URL. So the default stays `en_GB` for the UCITS lines and a fund overrides it
    # rather than this gaining a second endpoint — the payload shape is identical, which is
    # why `parse_ishares` needs no branch.
    params = {**BLACKROCK_PARAMS, "portfolioId": portfolio_id}
    locale = source.params.get("locale")
    if locale:
        params["locale"] = locale
    response = await client.get(BLACKROCK_URL, params=params)
    response.raise_for_status()
    return response.content


async def fetch_vanguard(
    client: httpx.AsyncClient, isin: str, source: FundSource
) -> List[bytes]:
    """
    Page until the API stops offering a `next`, returning one body per page, in order.

    The parser checks the assembled row count against the `size` the API declares, because a
    missing page is exactly the shape that makes a fund look like it holds only its largest
    names while the weights still sum plausibly.
    """
    ticker = source.params.get("ticker")
    if not ticker:
        raise FetchError(f"{source.symbol}: no ticker declared in app/etf_sources.py")

    bodies: List[bytes] = []
    start = 1
    for page in range(1, VANGUARD_MAX_PAGES + 1):
        if page > 1:
            await asyncio.sleep(BETWEEN_REQUESTS_S)
        response = await client.get(
            VANGUARD_URL.format(ticker=ticker),
            params={"start": start, "count": VANGUARD_PAGE},
        )
        response.raise_for_status()
        bodies.append(response.content)

        try:
            payload = response.json()
        except ValueError:
            # Not JSON: let the parser refuse it with its own message rather than guessing
            # here. The bytes are kept, which is the point of separating the two halves.
            break
        if not (payload.get("next") or {}).get("href"):
            break
        start += VANGUARD_PAGE
    else:
        raise FetchError(
            f"{source.symbol}: still paginating after {VANGUARD_MAX_PAGES} pages — refusing "
            f"to keep going"
        )
    return bodies


async def fetch_invesco(client: httpx.AsyncClient, isin: str, source: FundSource) -> bytes:
    cusip = cusip_from_isin(isin)
    if not cusip:
        raise FetchError(
            f"{source.symbol}: {isin} is not a US ISIN, so no CUSIP can be taken from it — "
            f"this endpoint has no other key"
        )
    response = await client.get(
        INVESCO_URL.format(cusip=cusip), params=INVESCO_PARAMS,
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return response.content


async def fetch_first_trust(client: httpx.AsyncClient, source: FundSource) -> bytes:
    response = await client.get(FIRST_TRUST_URL, params={"Ticker": source.symbol})
    response.raise_for_status()
    return response.content


async def fetch_defiance(client: httpx.AsyncClient, source: FundSource) -> bytes:
    slug = source.params.get("slug") or source.symbol.lower()
    response = await client.get(DEFIANCE_URL.format(slug=slug))
    response.raise_for_status()
    return response.content


async def fetch_vaneck(client: httpx.AsyncClient, source: FundSource) -> bytes:
    """
    Two GETs: the product page to be given the geo/consent cookies, then the download.

    Without the first, the download URL bounces between locale and consent redirects until
    `follow_redirects` gives up — the failure looks like a network fault rather than a missing
    cookie, which is why it is worth a comment. `httpx.AsyncClient` keeps the jar itself.
    """
    slug = source.params.get("slug")
    if not slug:
        raise FetchError(f"{source.symbol}: no slug declared in app/etf_sources.py")

    await client.get(VANECK_CONSENT_URL.format(slug=slug))
    await asyncio.sleep(BETWEEN_REQUESTS_S)
    response = await client.get(VANECK_URL.format(slug=slug))
    response.raise_for_status()
    # Checked here as well as in the parser, so a consent page is named at the point it
    # arrived rather than as an obscure zip error two commands later.
    if not response.content.startswith(b"PK\x03\x04"):
        raise FetchError(
            f"{source.symbol}: the download is not an XLSX (it starts "
            f"{response.content[:16]!r}) — the consent cookies were probably not accepted"
        )
    return response.content


async def fetch_bodies(
    client: httpx.AsyncClient, isin: str, source: FundSource
) -> List[bytes]:
    """
    The raw response body (or bodies, for a paginated source) for one fund.

    Raises `FetchError` for an adapter with no fetcher, so a caller cannot mistake "no
    route" for "empty basket".
    """
    adapter = source.adapter
    if adapter == "dws":
        return [await fetch_dws(client, isin)]
    if adapter == "blackrock":
        return [await fetch_blackrock(client, isin, source)]
    if adapter == "vanguard_us":
        return await fetch_vanguard(client, isin, source)
    if adapter == "invesco":
        return [await fetch_invesco(client, isin, source)]
    if adapter == "first_trust":
        return [await fetch_first_trust(client, source)]
    if adapter == "defiance":
        return [await fetch_defiance(client, source)]
    if adapter == "vaneck":
        return [await fetch_vaneck(client, source)]
    raise FetchError(
        f"{source.symbol}: adapter {adapter!r} has no fetcher — download it by hand and use "
        f"import_etf_basket"
    )


def parse_bodies(
    fund_isin: str, bodies: Sequence[bytes], adapter: str, symbol: str, fetched_on: date
) -> ParsedBasket:
    """
    Dispatch to the adapter's parser. Pure.

    `symbol` reaches the two HTML adapters because their routes are keyed by ticker in a query
    string or a path segment, so a redirect can serve another fund's holdings under a 200 —
    they check it against the page's `<title>`, which is their equivalent of the
    `ShareClass ISIN` echo `parse_dws` checks on every row.

    `fetched_on` is the as-of only for issuers that publish none (Xtrackers) or publish one
    that runs ahead (Defiance). The CLI passes the file's own mtime, so importing a file saved
    three days ago cannot claim the basket is current; the scheduled refresh passes today,
    which is when the download actually happened.
    """
    single = len(bodies) == 1

    if adapter == "vanguard_us":
        return parse_vanguard_us(list(bodies), fund_isin)
    if not single:
        raise BasketParseError(
            f"{adapter} responses are a single file; {len(bodies)} were given"
        )
    if adapter == "dws":
        return parse_dws(bodies[0], fund_isin, fetched_on)
    if adapter == "blackrock":
        return parse_ishares(bodies[0], fund_isin)
    if adapter == "invesco":
        return parse_invesco(bodies[0], fund_isin)
    if adapter == "first_trust":
        return parse_first_trust(bodies[0], fund_isin, symbol)
    if adapter == "defiance":
        return parse_defiance(bodies[0], fund_isin, symbol, fetched_on)
    if adapter == "vaneck":
        return parse_vaneck(bodies[0], fund_isin)
    raise BasketParseError(
        f"no parser for adapter {adapter!r}. Known: {', '.join(sorted(PARSERS))} — pass "
        f"--adapter to override what app/etf_sources.py declares"
    )
