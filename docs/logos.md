# Holding logos — logo.dev, `security_logos`, `/api/logos`

> **This file is authoritative for its subsystem.** Keep it accurate in the same change that
> moves the code. GitHub issue #12.

Each holding's round avatar (`frontend/src/components/ui/TickerAvatar.tsx`) shows the company's
logo **mark** — Google's "G", not the "Google" wordmark — when one is stored, and the ticker
monogram otherwise. **Where:** by `securityId` on Positions, the insights strip, Dividends and its
calendar, Activity (`/api/portfolio/activity` rows carry `security_id`, null for a cash flow),
Fundamentals, the Allocation drill-down (`/api/allocation/portfolio` positions carry
`security_id`, the first listing's when two merge), Rebalance and the Watchlist when the ticker is
one of ours; by `isin` on Tax, Look-through constituents and any other Watchlist name.

**Watchlist names never held** have no ISIN of their own: `app/services/ticker_isin.py` reads one
from the stored fund baskets — ~10,000 constituents published with ticker and ISIN — normalising
each issuer's spelling ("BRK.B" / "BRKB" / "BRK B", Tencent's "700" for `0700.HK`) and narrowing
the ISIN's country by the Yahoo suffix (bare = US, where an ADR's ISIN lives too; `.TW` = TW, so
"2330" is never the Saudi line an EM basket also calls "2330"); the heaviest match wins and an
unknown suffix resolves nothing. `/api/watchlist` rows carry `security_id` (ours) or `isin`, and
the logo pass targets those ISINs too, so a name added to the watchlist gets its logo at the next
05:00 pass.

**What a watchlist response costs is bounded by the watchlist, not the security table** (issue
#32). `WatchlistService.security_ids_by_ticker` builds its held map and its full map together,
in two queries: the eligible securities, held ones first, and the active mappings once, through
`MarketDataService.yahoo_tickers_for` ([pricing-and-fx.md](pricing-and-fx.md)). Before, it ran
one `ticker_mappings` SELECT per security ever held. Held first decides a tie: when a sold and a
held security resolve to the same ticker (a conid change, a recycled symbol), the row carries the
held one's id, and so its logo. `isins_by_yahoo_ticker` narrows the basket read in SQL to lines
whose ticker contains the asked base's characters in order (`LIKE '%B%R%K%B%'`, a superset of
what normalises to it) and whose ISIN country the suffix admits. Python still decides. It asks
nothing when every row is ours. POST shares one service between its sync (`_held_closes`) and
its response, so the index is built once. `tests/test_watchlist_read_cost.py` pins the statement
count exactly.

**A CN ISIN is told apart by code length** (`CN_CODE_DIGITS`, issue #27). `.HK` admits CN ISINs
because H-shares carry `CNE…` ones, but so does every mainland A-share, and with the zeros
stripped Vanke's A-share "000002" is CLP's `0002.HK`; the heavier line won, and with only the
A-share stored CLP's row got Vanke's logo. A CN ISIN now counts under `.HK` only when the
issuer's code had at most five digits, and under `.SZ`/`.SS` only when it had six. That rule
covers the mirror case too: a stored H-share "939" never resolves `000939.SZ`. It assumes the
issuers write A-share codes with their six digits. An issuer that strips the zeros defeats it:
under `.SZ`/`.SS` that line resolves nothing, but under `.HK` it reads as a Hong Kong code again.
The other suffixes that share a country (`.TW`/`.TWO`,
`.KS`/`.KQ`) draw on one national code list each, so stripping zeros cannot merge two companies.

## The privacy decision (owner, 2026-10-17)

Until 2026-10-17 the avatar was a monogram by design: the CSP allows images from our own origin
only, and a logo service would learn every holding. **The owner reversed that.** logo.dev now
learns the held ISINs (and a ticker when the ISIN is unknown to it); the trade-off is accepted.
The browser still never talks to logo.dev: the server fetches, stores and serves the bytes, so
the CSP stays `img-src 'self' data:` (`backend/frontend-nginx.conf`).

## Source: logo.dev's API (owner's choice)

From logo.dev's docs and terms, read 2026-10-17:

- `GET https://api.logo.dev/v2/brands/logo?isin=…` (or `ticker=…`) with
  `Authorization: Bearer <secret key sk_…>` answers in a `{data, metadata}` envelope: the
  signed download URL is **`data.url`** (valid ~24 h, sent with no key), `data.status` is
  `ready`. The first live pass (2026-10-17) read a top-level `url`, found none, and stored
  nothing; `test_the_lookup_reads_the_documented_envelope` pins the documented body. The
  download takes `size` (max 800), `format` (jpg/png/webp; jpg without one) and `theme`. We ask
  `size=128&format=png`: the square mark on a transparent background, 2x+ the 32–36 px avatar.
- "On every plan, you can store anything the Logo.dev API returns." The signed `data.url` is
  the documented way to fetch that image; the plain CDN (`img.logo.dev/<domain>?token=…`) must
  not be copied or proxied, and we never call it.
- No attribution is needed for a personal project that earns nothing — so the UI carries none.
- Unknown identifier: 404 `not_found`, free. Not indexed yet: 202 with `Retry-After: 30`, free;
  usually ready within a minute. Brand found but no logo: 404, billed.
- **Billing:** the lookup costs 2 credits, once per brand per 24 hours; the download from
  `data.url` is free. The free Community plan carries $5 of API credits a month and 100
  requests a minute. Spent credits answer 402.
- **Rejected: Brandfetch.** Its free Logo API forbids downloading or caching ("hotlink… don't
  download, cache, or re-host"), and our CSP cannot hotlink.

The first real run (2026-10-17) resolved every US, Dutch, German, Cayman, Korean and
Taiwanese ISIN and the US and Irish ETFs to a brand; the two pillar 3a funds (`CH…`) were
`not_found` under both ISIN and ticker, and SBI was still indexing. Whether an ETF's mark is
the issuer's is a visual check. For a miss, use the manual import below.

## Storage — `security_logos`

One row per **ISIN** (`app/models/security_logo.py`), so two listings of one company share one
image. Columns: `isin` (PK), `image` (`LargeBinary`, the schema's first), `content_type`,
`sha256`, `source` (`logo.dev` | `manual`), `fetched_at`, `checked_at`. Migration `a7d3c9e5f1b2`.

- **No row = never asked; a NULL `image` beside `checked_at` = asked, nothing usable** — the
  `IsinIdentity` pattern, so an ISIN logo.dev cannot serve is not re-bought every pass.
- A later "not found" or a refused image **keeps** an image already stored and only moves
  `checked_at`.
- **A `manual` row is never asked about and never overwritten** by the automatic pass, `--force`
  included; the repository's upsert carries the condition, so a pass in flight cannot replace a
  manual import either.

## Refuse whole — `logo_service.validate_logo`

One implementation for both write paths. PNG, JPEG or WebP only, by **magic bytes**, and the
server's Content-Type must name the same type; **never SVG** (served from our own origin, an SVG
can carry script); at most 100 KB (the download stops reading one byte past it); never empty. A
refused image stores nothing and is that ISIN's warning, never the run's status. The download URL
must be https, and the secret key is never sent to it (another host).

## The pass — bounded, because found logos are billed

`LogoService.refresh` (`app/services/logo_service.py`):

- **Every security with an ISIN, sold ones included**, held ones first (`security_repository.held()`
  orders the due list), never crypto, not gated by `price_source` (that is the Yahoo opt-out).
  Until 2026-10-17 it was held only, and the Activity tab — which lists every trade ever made —
  showed a monogram for every sold holding. Touches no Yahoo: the ticker fallback uses the price
  path's resolver (`MarketDataService._get_yahoo_ticker`), a database lookup.
- **Due** = no row, or `checked_at` older than `RECHECK_AFTER` (90 days); manual rows never.
- ISIN first; on `not_found` the security's Yahoo ticker. **At most `MAX_LOOKUPS_PER_PASS` (30)
  API lookups a pass**, each request paced `MIN_INTERVAL_S` (1 s) after the previous one
  finished — by `paced_http.PacedGet`, the one paced GET the crypto clients share too. The
  rest are deferred to the next pass.
- **401/402/403/429 from `api.logo.dev` stop the pass** with a warning (`STOP_STATUSES`, the API's
  answers only). **202 leaves the ISIN for the next pass**, never sleeping on `Retry-After`. A
  failed request (transport, a 5xx from the API) is not remembered, so it is asked again next
  pass; it is a warning.
- **A download never stops the pass.** The signed URL is another host and carries no key, so
  its 401/403/429 says nothing about the key or the credits. Until issue #66 it raised
  `LogoDevStop`: the pass stopped at an ISIN left unstamped, which sorted first again, so every
  pass re-bought that one lookup and stopped, and no ISIN after it was ever asked. Now **a 4xx
  from the download (401/402/403/404/410/429 …) is that ISIN's refusal** (`LogoRefused`):
  stamped (`checked_at`), a warning, and the pass goes on. **A 5xx or a 408, or a download that
  never answered (transport), stays retryable** (`is_retryable_download_status`): not stamped,
  a warning, the pass goes on to the next ISIN, and that ISIN is asked again next pass — bounded
  by the daily cadence, at the cost of re-buying its lookup.
- Writes commit as they happen, so a crash keeps what was already paid for.
- **No `LOGO_DEV_SECRET_KEY`: `skipped: not configured`, no warning, no query.** A warning that
  is always present teaches the reader to skip it.

**Where it runs:** the third step of the `fundamentals_refresh` job (05:00 Berlin, Tue–Sat;
docs/flex-and-sync.md, *Sync schedule*). It runs after a Yahoo rate limit or a crashed
fundamentals pass (different upstream), its own failure is a warning, never the job's status, and
it records inside that job's `sync_runs` row as `logos_result`. Nothing due costs zero requests.

**Cost:** at most 30 lookups a pass, five passes a week. After the first fill (every security
ever held, a few passes) a pass asks only about new securities and the ~1/90 of ISINs whose
re-check came due each day — cents a month. On the free plan spend cannot exceed the monthly credits: once they
are gone logo.dev answers 402 and every pass stops at its first lookup.

## Serving — `app/routers/logos.py`

- `GET /api/logos` → `{"logos": {"<security_id>": "<sha256[:12]>"}, "isins": {"<isin>": "…"}}`:
  every security whose ISIN has an image, and every stored image by ISIN — a look-through
  constituent filled ahead from a basket, or a watchlist row, carries an ISIN but no security.
  The browser never asks for a logo that does not exist.
- `GET /api/logos/isin/{isin}?v=<version>` → the same bytes and caching by ISIN.
- `GET /api/logos/{security_id}?v=<version>` → the bytes with `ETag` (the sha256),
  `X-Content-Type-Options: nosniff`, and `Cache-Control: public, max-age=31536000, immutable`
  **only when `v` is the current version** (`no-cache` otherwise, so a replaced logo is never
  pinned under its old URL); `If-None-Match` answers 304; 404 without an image.
- Reads are open. The manifest is rate-limited like every `/api/` read; **the images
  (`GET /api/logos/...`) are exempt from the limiter** (`EXEMPT_GET_PREFIXES` in
  `app/rate_limit.py`): one request per avatar, and on a cold cache a look-through page's hundred
  avatars used to spend the per-client budget and 429 the page's own data requests — a 429 on an
  image also flips that avatar to its monogram for the session. They are primary-key/ISIN lookups,
  served `immutable` once versioned.
- The id is `DbId` (`app/routers/_params.py`): 0, a negative or anything past int64 is a **422**
  from validation. Past int64 it used to reach the SQLite bind and answer 500 with a logged
  traceback per request, on this exempt route (#64). 404s get no limit of their own — one lookup and
  one access-log line each, fewer lines than a 429 writes (docs/flex-and-sync.md, *`/api/`
  protections*).
- Frontend: `lib/useLogos.ts` (TanStack Query, `staleTime` 1 h) and `TickerAvatar`
  (`securityId`, else `isin`): the image
  `object-contain` on a white circle with a hairline ring, in the monogram's box (no layout
  shift); `onError` falls back to the monogram. Outside a `QueryClientProvider` (a table's own
  unit tests) the avatar is the monogram.

## The CLIs — the write paths (no upload endpoint)

**Fetch now** instead of waiting for 05:00. `--dry-run` lists what is due and makes **no
request** (a dry run that asked would pay twice for the same answer); the real run prints each
ISIN's outcome. `--force` ignores the 90 days (never a manual row); `--isin X` asks about one
ISIN (repeatable) — a security carrying it, held or not, or else a basket constituent, so a
single Look-through company can be fetched without filling its whole basket; `--limit N` raises
the 30-lookup cap for one run. Records
`sync_type='manual_logo_fetch'`.

**Fill ahead from a fund basket:** `--basket FUND` (a fund ISIN or symbol, repeatable) asks about
the constituents of a stored basket instead, `--top N` the N heaviest of each. A fund that borrows
another's basket (`etf_sources.basket_proxy_isin`, e.g. the Swisscanto World ex CH → IWDA) reads
that basket. Logos are keyed by
ISIN, so a stock bought later shows its logo at once (`manifest` joins on the ISIN). The
scheduled pass never re-checks these — only securities — so they cost once. A constituent
unknown by ISIN falls back to the issuer's published ticker.

```bash
docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --dry-run
docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos
docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --basket QQQM --basket VT --top 100 --dry-run
```

**Filled 2026-10-18:** QQQM's whole basket (the Nasdaq-100), and the top 100 of the MSCI World
(IWDA, `IE00B4L5Y983`), of VT and of VOO (`US9229083632`, the S&P 500 basket kept as DBPG's
donor). Measured afterwards against the stored baskets: **100 of the S&P 500's top 100** and
**94 of the Nasdaq-100's 95** constituents with an ISIN carry a logo — the one miss, AGPXX, is a
money-market cash line. On the live listings: the Look-through company table's top 100 100/100
(ten from the thematic and EM baskets fetched by `--isin`), the Watchlist 24/24, Positions
35/35. 235 logos in all, every security ever held included. Re-measure with a
read-only join of `etf_holdings` (by `fund_isin`, heaviest first) on `security_logos.isin`.

**Import by hand** a logo logo.dev misses or gets wrong (e.g. a PNG from Flaticon, ≤ 100 KB,
square). The same `validate_logo`; the ISIN must belong to a security; stored as `manual`,
replacing what was there. Records `sync_type='manual_logo_import'`; a dry run records nothing.
`scp` the file to the VPS (or upload it with the Hostinger file manager), copy it into the
container, then:

```bash
docker cp /root/logo.png backend-portfolio-backend-1:/tmp/logo.png
docker exec backend-portfolio-backend-1 python -m app.cli.import_logo TW0002330008 /tmp/logo.png --dry-run
docker exec backend-portfolio-backend-1 python -m app.cli.import_logo TW0002330008 /tmp/logo.png
```

## Setup (owner)

1. Create a logo.dev account; copy the **secret** key (`sk_…`, not the publishable `pk_…`).
2. Add `LOGO_DEV_SECRET_KEY=sk_…` to `/root/IBKR_investment_tracker/backend/.env`.
3. `cd /root/IBKR_investment_tracker/backend && GIT_COMMIT=$(git -C /root/IBKR_investment_tracker rev-parse HEAD) docker compose up -d`
   — never `restart`, which does not re-read `.env` (docs/deployment.md); confirm on `/health`.
4. `fetch_logos --dry-run`, then without it; check the logos on Positions and Dividends.

Tests: `tests/test_logos.py` (refusals, a refused or failed download, budget, stop, re-ask, manual rows,
CLIs, routes),
`tests/test_scheduler_jobs.py` (the step), `tests/test_api_smoke.py` (both reads),
`frontend/src/components/ui/TickerAvatar.test.tsx`.
