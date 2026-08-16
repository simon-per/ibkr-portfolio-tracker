"""
Download the raw holdings file for one or more held funds. Writes files; touches no database.

**The network half only, deliberately.** `app/cli/import_etf_basket.py` parses and stores what
this writes, and keeping them apart is what makes every scraper's failure mode a committable
fixture — the same relationship `ingest_flex_xml.py` has to a statement downloaded from Client
Portal. It is also the only way to test the trap this feature is most exposed to: the retired
iShares holdings URL answers **HTTP 200 with `Content-Type: text/csv` and an HTML body**, so a
fetcher that parsed inline would have nothing to hand a test.

Touches **no** Yahoo and **no** IBKR, so neither rule at the top of CLAUDE.md applies. It does
reach seven issuer sites, as a guest rather than a customer: a descriptive User-Agent carrying
`LOOKTHROUGH_CONTACT_EMAIL` when set, one request at a time, a pause between them, and no
retry storm. These files are the funds' own regulatory portfolio disclosures — fine to cache
privately for one portfolio, and not ours to redistribute.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 \\
        python -m app.cli.fetch_etf_baskets --all --out /tmp/baskets
    docker exec backend-portfolio-backend-1 \\
        python -m app.cli.fetch_etf_baskets XNAS XAIX --out /tmp/baskets

Then, for each file it names:

    python -m app.cli.import_etf_basket <FUND_ISIN> /tmp/baskets/<file> --dry-run
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import httpx

from app.etf_sources import FUND_SOURCES, FundSource, user_agent
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


# Every adapter `--all` will attempt. Derived from the fetchers below rather than from
# `etf_sources.ADAPTERS`, which also names `manual` — a real answer, not a route.
AUTOMATED_ADAPTERS = frozenset({
    "dws", "blackrock", "vanguard_us", "invesco", "first_trust", "defiance", "vaneck",
})


class FetchError(Exception):
    """The download failed — reported, and no file written for that fund."""


def _resolve_targets(names: List[str], fetch_all: bool) -> List[Tuple[str, FundSource]]:
    """
    Map symbols or ISINs onto registry entries, refusing anything unknown.

    Funds whose only route is a hand-downloaded file are skipped with a note rather than
    treated as an error: `adapter="manual"` is a real answer, not a gap.
    """
    if fetch_all:
        return sorted(
            ((isin, src) for isin, src in FUND_SOURCES.items()
             if src.adapter in AUTOMATED_ADAPTERS),
            key=lambda pair: pair[1].symbol,
        )

    by_symbol = {src.symbol.upper(): (isin, src) for isin, src in FUND_SOURCES.items()}
    out: List[Tuple[str, FundSource]] = []
    for name in names:
        key = name.strip().upper()
        if key in FUND_SOURCES:
            out.append((key, FUND_SOURCES[key]))
        elif key in by_symbol:
            out.append(by_symbol[key])
        else:
            raise FetchError(
                f"{name!r} is neither a fund ISIN nor a symbol in app/etf_sources.py"
            )
    return out


async def _fetch_dws(client: httpx.AsyncClient, isin: str, out_dir: Path) -> List[Path]:
    response = await client.get(DWS_URL.format(isin=isin))
    response.raise_for_status()
    path = out_dir / f"{isin}.dws.csv"
    path.write_bytes(response.content)
    return [path]


async def _fetch_blackrock(
    client: httpx.AsyncClient, isin: str, source: FundSource, out_dir: Path
) -> List[Path]:
    portfolio_id = source.params.get("portfolio_id")
    if not portfolio_id:
        raise FetchError(f"{source.symbol}: no portfolio_id declared in app/etf_sources.py")
    response = await client.get(
        BLACKROCK_URL, params={**BLACKROCK_PARAMS, "portfolioId": portfolio_id}
    )
    response.raise_for_status()
    path = out_dir / f"{isin}.blackrock.json"
    path.write_bytes(response.content)
    return [path]


async def _fetch_vanguard(
    client: httpx.AsyncClient, isin: str, source: FundSource, out_dir: Path
) -> List[Path]:
    """
    Page until the API stops offering a `next`, writing one file per page.

    The page files are numbered so `import_etf_basket` can be handed them in order: the parser
    checks the assembled row count against the `size` the API declares, because a missing page
    is exactly the shape that makes a fund look like it holds only its largest names while the
    weights still sum plausibly.
    """
    ticker = source.params.get("ticker")
    if not ticker:
        raise FetchError(f"{source.symbol}: no ticker declared in app/etf_sources.py")

    paths: List[Path] = []
    start = 1
    for page in range(1, VANGUARD_MAX_PAGES + 1):
        if page > 1:
            await asyncio.sleep(BETWEEN_REQUESTS_S)
        response = await client.get(
            VANGUARD_URL.format(ticker=ticker),
            params={"start": start, "count": VANGUARD_PAGE},
        )
        response.raise_for_status()
        path = out_dir / f"{isin}.vanguard_us.{page:02d}.json"
        path.write_bytes(response.content)
        paths.append(path)

        try:
            payload = response.json()
        except ValueError:
            # Not JSON: let the parser refuse it with its own message rather than guessing
            # here. The bytes are on disk, which is the point of this command.
            break
        if not (payload.get("next") or {}).get("href"):
            break
        start += VANGUARD_PAGE
    else:
        raise FetchError(
            f"{source.symbol}: still paginating after {VANGUARD_MAX_PAGES} pages — refusing "
            f"to keep going"
        )
    return paths


async def _fetch_invesco(
    client: httpx.AsyncClient, isin: str, source: FundSource, out_dir: Path
) -> List[Path]:
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
    path = out_dir / f"{isin}.invesco.json"
    path.write_bytes(response.content)
    return [path]


async def _fetch_first_trust(
    client: httpx.AsyncClient, isin: str, source: FundSource, out_dir: Path
) -> List[Path]:
    response = await client.get(FIRST_TRUST_URL, params={"Ticker": source.symbol})
    response.raise_for_status()
    path = out_dir / f"{isin}.first_trust.html"
    path.write_bytes(response.content)
    return [path]


async def _fetch_defiance(
    client: httpx.AsyncClient, isin: str, source: FundSource, out_dir: Path
) -> List[Path]:
    slug = source.params.get("slug") or source.symbol.lower()
    response = await client.get(DEFIANCE_URL.format(slug=slug))
    response.raise_for_status()
    path = out_dir / f"{isin}.defiance.html"
    path.write_bytes(response.content)
    return [path]


async def _fetch_vaneck(
    client: httpx.AsyncClient, isin: str, source: FundSource, out_dir: Path
) -> List[Path]:
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
    path = out_dir / f"{isin}.vaneck.xlsx"
    path.write_bytes(response.content)
    return [path]


async def fetch_baskets(
    names: List[str], out_dir: Path, fetch_all: bool = False
) -> int:
    try:
        targets = _resolve_targets(names, fetch_all)
    except FetchError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    if not targets:
        print("Nothing to fetch.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": user_agent(), "Accept": "*/*"}
    written: Dict[str, List[Path]] = {}
    failures: Dict[str, str] = {}

    async with httpx.AsyncClient(
        headers=headers, timeout=REQUEST_TIMEOUT_S, follow_redirects=True
    ) as client:
        for index, (isin, source) in enumerate(targets):
            if index:
                await asyncio.sleep(BETWEEN_REQUESTS_S)
            try:
                if source.adapter == "dws":
                    paths = await _fetch_dws(client, isin, out_dir)
                elif source.adapter == "blackrock":
                    paths = await _fetch_blackrock(client, isin, source, out_dir)
                elif source.adapter == "vanguard_us":
                    paths = await _fetch_vanguard(client, isin, source, out_dir)
                elif source.adapter == "invesco":
                    paths = await _fetch_invesco(client, isin, source, out_dir)
                elif source.adapter == "first_trust":
                    paths = await _fetch_first_trust(client, isin, source, out_dir)
                elif source.adapter == "defiance":
                    paths = await _fetch_defiance(client, isin, source, out_dir)
                elif source.adapter == "vaneck":
                    paths = await _fetch_vaneck(client, isin, source, out_dir)
                else:
                    print(
                        f"{source.symbol}: adapter {source.adapter!r} has no fetcher — "
                        f"download it by hand and use import_etf_basket"
                    )
                    continue
            except Exception as e:
                failures[source.symbol] = str(e)
                print(f"{source.symbol}: FAILED - {e}", file=sys.stderr)
                continue

            written[source.symbol] = paths
            total = sum(p.stat().st_size for p in paths)
            print(
                f"{source.symbol} ({isin}): {len(paths)} file(s), {total:,} bytes -> "
                f"{paths[0].name}" + (f" .. {paths[-1].name}" if len(paths) > 1 else "")
            )

    print()
    print(f"Fetched {len(written)} fund(s), {len(failures)} failed.")
    if written:
        print("Import them with, for each fund:")
        for symbol, paths in written.items():
            isin = next(i for i, s in FUND_SOURCES.items() if s.symbol == symbol)
            # A glob for the paginated sources: VT is 21 pages, and printing 21 absolute
            # paths produces a line nobody can read, let alone copy. The shell expands the
            # glob in page order because the page number is zero-padded.
            target = (
                str(paths[0])
                if len(paths) == 1
                else str(paths[0].parent / f"{isin}.{paths[0].name.split('.')[1]}.*.json")
            )
            print(f"  python -m app.cli.import_etf_basket {isin} {target} --dry-run")
    # A partial run is not a failure: each fund is independent, and the ones that arrived are
    # worth importing.
    return 1 if failures and not written else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("funds", nargs="*", help="Fund symbols or ISINs (default: none)")
    parser.add_argument("--all", action="store_true", dest="fetch_all",
                        help="Every fund with an automated adapter")
    parser.add_argument("--out", type=Path, required=True,
                        help="Directory to write the raw response bodies into")
    args = parser.parse_args()

    if not args.funds and not args.fetch_all:
        parser.error("name at least one fund, or pass --all")

    return asyncio.run(fetch_baskets(args.funds, args.out, fetch_all=args.fetch_all))


if __name__ == "__main__":
    raise SystemExit(main())
