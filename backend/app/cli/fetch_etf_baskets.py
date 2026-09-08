"""
Download the raw holdings file for one or more held funds. Writes files; touches no database.

**The network half only, deliberately.** `app/cli/import_etf_basket.py` parses and stores what
this writes, and keeping them apart is what makes every scraper's failure mode a committable
fixture — the same relationship `ingest_flex_xml.py` has to a statement downloaded from Client
Portal. It is also the only way to test the trap this feature is most exposed to: the retired
iShares holdings URL answers **HTTP 200 with `Content-Type: text/csv` and an HTML body**, so a
fetcher that parsed inline would have nothing to hand a test.

Since 2026-09-08 the 18:00 `full_sync` job refreshes stale baskets on its own through the same
fetchers (`app/services/etf_basket_fetch.py` — one implementation, two callers), so this is
the by-hand route: for a fund whose refresh keeps failing, where the saved body is what makes
the failure debuggable, and for a first import of a newly declared fund before the evening.

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

from app.etf_sources import FUND_SOURCES, FundSource
from app.services.etf_basket_fetch import (  # noqa: F401 — re-exported for callers and tests
    AUTOMATED_ADAPTERS,
    BETWEEN_REQUESTS_S,
    FetchError,
    fetch_blackrock,
    fetch_bodies,
    new_client,
)

logger = logging.getLogger(__name__)

# File suffix per adapter, so the import command can be read off the file name.
_EXTENSIONS = {
    "dws": "csv", "blackrock": "json", "vanguard_us": "json", "invesco": "json",
    "first_trust": "html", "defiance": "html", "vaneck": "xlsx",
}


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


def _write_bodies(isin: str, adapter: str, bodies: List[bytes], out_dir: Path) -> List[Path]:
    """
    One file per body, numbered when there is more than one.

    Paginated pages are zero-padded so `import_etf_basket` can be handed them in order by a
    shell glob; the parser checks the assembled row count against the `size` the API declares,
    because a missing page is exactly the shape that makes a fund look like it holds only its
    largest names while the weights still sum plausibly.
    """
    ext = _EXTENSIONS.get(adapter, "bin")
    paths: List[Path] = []
    if len(bodies) == 1:
        path = out_dir / f"{isin}.{adapter}.{ext}"
        path.write_bytes(bodies[0])
        return [path]
    for page, body in enumerate(bodies, start=1):
        path = out_dir / f"{isin}.{adapter}.{page:02d}.{ext}"
        path.write_bytes(body)
        paths.append(path)
    return paths


async def _fetch_blackrock(
    client: httpx.AsyncClient, isin: str, source: FundSource, out_dir: Path
) -> List[Path]:
    """File-writing wrapper around the shared fetcher, kept for the registry test's probe."""
    return _write_bodies(isin, "blackrock", [await fetch_blackrock(client, isin, source)], out_dir)


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
    written: Dict[str, List[Path]] = {}
    failures: Dict[str, str] = {}

    async with new_client() as client:
        for index, (isin, source) in enumerate(targets):
            if index:
                await asyncio.sleep(BETWEEN_REQUESTS_S)
            if source.adapter not in AUTOMATED_ADAPTERS:
                print(
                    f"{source.symbol}: adapter {source.adapter!r} has no fetcher — "
                    f"download it by hand and use import_etf_basket"
                )
                continue
            try:
                bodies = await fetch_bodies(client, isin, source)
                paths = _write_bodies(isin, source.adapter, list(bodies), out_dir)
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
